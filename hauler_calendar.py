"""
Hauler Calendar -> Discord

Reads one or more Google Calendar secret iCal feeds, draws month views
(multi-day trips shown as solid bars, Google-Calendar style) and keeps a
single Discord message updated with the pictures via a webhook.

Configuration (environment variables / GitHub secrets):
  CALENDARS    One calendar per line:   Name | https://...basic.ics | #hexcolor
               Name and color are optional. A bare URL also works.
  WEBHOOK_URL  Discord channel webhook URL.
  MONTHS       How many months to show, starting with the current one (default 3).
  TIMEZONE     IANA timezone (default America/Los_Angeles).

Local preview without Discord:
  python hauler_calendar.py --preview --ics sample.ics
"""

from __future__ import annotations

import argparse
import calendar as pycal
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dateutil import rrule
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
OUT_DIR = ROOT / "out"

# Google Calendar-ish colors: (fill, text)
PALETTE = [
    ("#039BE5", "#FFFFFF"),  # peacock
    ("#F4511E", "#FFFFFF"),  # tangerine
    ("#33B679", "#FFFFFF"),  # basil
    ("#8E24AA", "#FFFFFF"),  # grape
    ("#E67C73", "#FFFFFF"),  # flamingo
    ("#3F51B5", "#FFFFFF"),  # blueberry
    ("#F6BF26", "#202124"),  # banana
    ("#616161", "#FFFFFF"),  # graphite
    ("#D50000", "#FFFFFF"),  # tomato
    ("#0B8043", "#FFFFFF"),  # sage
    ("#7986CB", "#FFFFFF"),  # lavender
    ("#795548", "#FFFFFF"),  # cocoa
    ("#E91E63", "#FFFFFF"),  # pink
    ("#009688", "#FFFFFF"),  # teal
    ("#AFB42B", "#FFFFFF"),  # citron
    ("#FF8F00", "#FFFFFF"),  # amber
]

# ---------------------------------------------------------------- layout
W = 1200
MARGIN = 28
HEADER_H = 108
WEEKDAY_H = 38
DATE_H = 36
BAR_H = 30
BAR_GAP = 4
MIN_LANES = 3      # empty weeks still get room
MAX_LANES = 12     # weeks grow up to this many stacked trips
LEGEND_LINE = 30
ROW_PAD = 8
COL_W = (W - 2 * MARGIN) / 7

BG = "#FFFFFF"
GRID = "#DADCE0"
TEXT = "#202124"
MUTED = "#80868B"
OUTSIDE_BG = "#F8F9FA"
TODAY = "#1A73E8"


def _font(size: int, bold: bool = False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    dirs = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", ""]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(os.path.join(d, n) if d else n, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


F_TITLE = _font(42, bold=True)
F_SUB = _font(20, bold=True)
F_WEEKDAY = _font(17, bold=True)
F_DATE = _font(19)
F_DATE_B = _font(19, bold=True)
F_BAR = _font(18, bold=True)
F_MORE = _font(16, bold=True)


# ---------------------------------------------------------------- ICS parsing
@dataclass
class Trip:
    title: str
    start: dt.date          # first day (inclusive)
    end: dt.date            # last day (exclusive)
    time_label: str         # "6a" for timed events, "" for all-day
    cal_index: int


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_prop(line: str):
    # NAME;PARAM=x;PARAM="a:b":VALUE  -> split at first colon outside quotes
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(v: str) -> str:
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\")).strip()


def _parse_dt(value: str, params: dict, local: ZoneInfo):
    """Return a date (all-day) or an aware datetime."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date()
    if value.endswith("Z"):
        return dt.datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
    naive = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tz = local
    if "TZID" in params:
        try:
            tz = ZoneInfo(params["TZID"])
        except Exception:
            tz = local
    return naive.replace(tzinfo=tz)


def _parse_duration(v: str) -> dt.timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return dt.timedelta(0)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x) if x else 0 for x in m.groups()[1:])
    return sign * dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _key(x, local: ZoneInfo) -> str:
    """Normalize a date/datetime so EXDATE / RECURRENCE-ID can be matched."""
    if isinstance(x, dt.datetime):
        return x.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return x.strftime("%Y%m%d")


def parse_ics(text: str, cal_index: int, local: ZoneInfo,
              win_start: dt.date, win_end: dt.date) -> list[Trip]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"EXDATE": []}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        p = _split_prop(line)
        if not p:
            continue
        name, params, value = p
        if name == "EXDATE":
            for v in value.split(","):
                try:
                    cur["EXDATE"].append(_parse_dt(v, params, local))
                except ValueError:
                    pass
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            try:
                cur[name] = _parse_dt(value, params, local)
            except ValueError:
                pass
        elif name in ("SUMMARY", "RRULE", "DURATION", "STATUS", "UID"):
            cur[name] = value

    # Moved/edited single occurrences of repeating events
    overridden = {(e.get("UID"), _key(e["RECURRENCE-ID"], local))
                  for e in events if "RECURRENCE-ID" in e}

    trips: list[Trip] = []
    for e in events:
        if "DTSTART" not in e or e.get("STATUS", "").upper() == "CANCELLED":
            continue
        start = e["DTSTART"]
        all_day = not isinstance(start, dt.datetime)
        if "DTEND" in e:
            length = e["DTEND"] - start
        elif "DURATION" in e:
            length = _parse_duration(e["DURATION"])
        else:
            length = dt.timedelta(days=1) if all_day else dt.timedelta(0)

        starts = [start]
        if "RRULE" in e and "RECURRENCE-ID" not in e:
            starts = _expand(e, start, all_day, local, win_start, win_end, overridden)

        title = _unescape(e.get("SUMMARY", "(no title)")) or "(no title)"
        for s in starts:
            trip = _to_trip(title, s, s + length, all_day, local, cal_index)
            if trip and trip.end > win_start and trip.start < win_end:
                trips.append(trip)
    return trips


def _expand(e, start, all_day, local, win_start, win_end, overridden):
    rule = e["RRULE"]
    if all_day:
        # all-day series use naive midnight datetimes; make UNTIL naive too
        rule = re.sub(r"UNTIL=(\d{8})(T\d{6}Z?)?", r"UNTIL=\1", rule)
        dtstart = dt.datetime.combine(start, dt.time())
        lo = dt.datetime.combine(win_start - dt.timedelta(days=60), dt.time())
        hi = dt.datetime.combine(win_end, dt.time())
    else:
        rule = re.sub(r"UNTIL=(\d{8})(?=;|$)", r"UNTIL=\1T235959Z", rule)
        dtstart = start
        lo = dt.datetime.combine(win_start - dt.timedelta(days=60), dt.time(), local)
        hi = dt.datetime.combine(win_end, dt.time(), local)
    try:
        occ = rrule.rrulestr("RRULE:" + rule, dtstart=dtstart).between(lo, hi, inc=True)
    except Exception as ex:  # odd rule: just show the first occurrence
        print(f"warning: could not expand repeat rule {rule!r}: {ex}", file=sys.stderr)
        return [start]
    ex = {_key(x, local) for x in e["EXDATE"]}
    out = []
    for o in occ:
        o = o.date() if all_day else o
        k = _key(o, local)
        if k in ex or (e.get("UID"), k) in overridden:
            continue
        out.append(o)
    return out


def _to_trip(title, s, e, all_day, local, cal_index) -> Trip | None:
    if all_day:
        end = e if e > s else s + dt.timedelta(days=1)
        return Trip(title, s, end, "", cal_index)
    sl, el = s.astimezone(local), e.astimezone(local)
    first = sl.date()
    if el <= sl:
        last_excl = first + dt.timedelta(days=1)
    else:
        last_excl = (el - dt.timedelta(microseconds=1)).date() + dt.timedelta(days=1)
    h = sl.hour % 12 or 12
    label = f"{h}{'' if sl.minute == 0 else f':{sl.minute:02d}'}{'a' if sl.hour < 12 else 'p'}"
    return Trip(title, first, last_excl, label, cal_index)


# ---------------------------------------------------------------- drawing
def _fit(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _layout_week(order: list[Trip], ws: dt.date, we: dt.date):
    """Give each trip in this week a lane (row) so overlapping trips stack, Google-style."""
    lanes: list[list[tuple[int, int]]] = []
    placed = []
    for t in order:
        if t.end <= ws or t.start >= we:
            continue
        c0 = max((t.start - ws).days, 0)
        c1 = min((t.end - ws).days, 7)  # exclusive
        for li, used in enumerate(lanes):
            if all(c1 <= a or c0 >= b for a, b in used):
                lane = li
                break
        else:
            lanes.append([])
            lane = len(lanes) - 1
        lanes[lane].append((c0, c1))
        placed.append((t, lane, c0, c1))
    return placed, len(lanes)


def render_month(year: int, month: int, trips: list[Trip], cals: list[dict],
                 today: dt.date) -> Image.Image:
    cal = pycal.Calendar(firstweekday=6)  # Sunday first, like Google
    weeks = cal.monthdatescalendar(year, month)
    month_days = {dt.date(year, month, n) for n in range(1, pycal.monthrange(year, month)[1] + 1)}
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # --- legend: one chip per truck, wrapping onto as many lines as needed
    legend, x, line = [], MARGIN, 0
    for i, c in enumerate(cals):
        booked = len({t.start + dt.timedelta(days=k)
                      for t in trips if t.cal_index == i
                      for k in range((t.end - t.start).days)} & month_days)
        label = f"{c['name']}: {booked}/{len(month_days)} days"
        w = 26 + scratch.textlength(label, font=F_SUB)
        if x + w > W - MARGIN and x > MARGIN:
            x, line = MARGIN, line + 1
        legend.append((x, line, c["fill"], label))
        x += w + 34
    legend_lines = line + 1 if cals else 0
    header_h = HEADER_H + (legend_lines - 1) * LEGEND_LINE

    # --- rows grow to fit the busiest week (so nothing gets hidden)
    order = sorted(trips, key=lambda t: (t.start, -(t.end - t.start).days, t.cal_index,
                                         t.time_label or "", t.title))
    layouts = []
    for week in weeks:
        ws, we = week[0], week[-1] + dt.timedelta(days=1)
        placed, n = _layout_week(order, ws, we)
        shown = min(max(n, MIN_LANES), MAX_LANES)
        more = n > MAX_LANES
        h = DATE_H + shown * (BAR_H + BAR_GAP) + ROW_PAD + (22 if more else 0)
        layouts.append((week, ws, we, placed, h))

    grid_top = header_h + WEEKDAY_H
    H = grid_top + sum(l[4] for l in layouts) + MARGIN
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # header
    d.text((MARGIN, 12), dt.date(year, month, 1).strftime("%B %Y"), font=F_TITLE, fill=TEXT)
    for lx, ln, fill, label in legend:
        ly = 68 + ln * LEGEND_LINE
        d.rounded_rectangle((lx, ly + 2, lx + 18, ly + 20), radius=4, fill=fill)
        d.text((lx + 26, ly), label, font=F_SUB, fill=TEXT)

    for i, name in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]):
        cx = MARGIN + i * COL_W
        tw = d.textlength(name, font=F_WEEKDAY)
        d.text((cx + (COL_W - tw) / 2, header_h + 8), name, font=F_WEEKDAY, fill=MUTED)

    ry = grid_top
    for week, ws, we, placed, row_h in layouts:
        # cells
        for c, day in enumerate(week):
            cx = MARGIN + c * COL_W
            if day.month != month:
                d.rectangle((cx, ry, cx + COL_W, ry + row_h), fill=OUTSIDE_BG)
            label = str(day.day) if day.day != 1 else day.strftime("%b 1")
            f = F_DATE_B if day == today else F_DATE
            tw = d.textlength(label, font=f)
            tx = cx + (COL_W - tw) / 2
            if day == today:
                rad = max(14, tw / 2 + 8)
                mid = cx + COL_W / 2
                d.rounded_rectangle((mid - rad, ry + 4, mid + rad, ry + 33), radius=14, fill=TODAY)
                d.text((tx, ry + 8), label, font=f, fill="#FFFFFF")
            else:
                d.text((tx, ry + 8), label, font=f, fill=TEXT if day.month == month else MUTED)

        d.line((MARGIN, ry, W - MARGIN, ry), fill=GRID)
        for c in range(8):
            cx = MARGIN + c * COL_W
            d.line((cx, ry, cx, ry + row_h), fill=GRID)

        hidden = [0] * 7
        for t, lane, c0, c1 in placed:
            if lane >= MAX_LANES:
                for c in range(c0, c1):
                    hidden[c] += 1
                continue
            fill, txt = cals[t.cal_index]["fill"], cals[t.cal_index]["text"]
            cont_left, cont_right = t.start < ws, t.end > we
            bx0 = MARGIN + c0 * COL_W + (1 if cont_left else 5)
            bx1 = MARGIN + c1 * COL_W - (1 if cont_right else 5)
            by0 = ry + DATE_H + lane * (BAR_H + BAR_GAP)
            by1 = by0 + BAR_H
            d.rounded_rectangle((bx0, by0, bx1, by1), radius=5, fill=fill)
            if cont_left:
                d.rectangle((bx0, by0, bx0 + 6, by1), fill=fill)
            if cont_right:
                d.rectangle((bx1 - 6, by0, bx1, by1), fill=fill)
            prefix = "◀ " if cont_left else ""
            text = prefix + (f"{t.time_label} " if t.time_label and not cont_left else "") + t.title
            d.text((bx0 + 7, by0 + 5), _fit(d, text, F_BAR, bx1 - bx0 - 14), font=F_BAR, fill=txt)

        for c in range(7):
            if hidden[c]:
                d.text((MARGIN + c * COL_W + 8, ry + DATE_H + MAX_LANES * (BAR_H + BAR_GAP) + 1),
                       f"+{hidden[c]} more", font=F_MORE, fill=MUTED)
        ry += row_h

    d.line((MARGIN, ry, W - MARGIN, ry), fill=GRID)
    return img


# ---------------------------------------------------------------- config
def parse_calendars(raw: str) -> list[dict]:
    cals = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 1:
            name, url, color = f"Hauler {len(cals) + 1}", parts[0], ""
        else:
            name, url = parts[0], parts[1]
            color = parts[2] if len(parts) > 2 else ""
        fill, text = PALETTE[len(cals) % len(PALETTE)]
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            fill, text = color, "#FFFFFF"
        cals.append({"name": name, "url": url, "fill": fill, "text": text})
    return cals


def add_months(y: int, m: int, n: int) -> tuple[int, int]:
    m0 = m - 1 + n
    return y + m0 // 12, m0 % 12 + 1


# ---------------------------------------------------------------- discord
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _multipart(content: str, files: list[tuple[str, Path]]):
    """files: (name shown in Discord, local path)"""
    payload = {
        "content": content,
        "embeds": [],
        "attachments": [{"id": i, "filename": name} for i, (name, _) in enumerate(files)],
        "allowed_mentions": {"parse": []},
    }
    return [("payload_json", (None, json.dumps(payload), "application/json"))] + [
        (f"files[{i}]", (name, path.read_bytes(), "image/png")) for i, (name, path) in enumerate(files)
    ]


def post_to_discord(webhook: str, files: list[Path]) -> None:
    """
    Message 1: this month, big and visible.
    Message 2: the following months as tap-to-open (spoiler) images.
    Both messages are edited in place on every run, so the channel never fills up.
    """
    webhook = webhook.split("?")[0].rstrip("/")
    ts = int(dt.datetime.now(dt.timezone.utc).timestamp())

    def label(p: Path) -> str:
        y, m = p.stem.split("_")[1:3]
        return dt.date(int(y), int(m), 1).strftime("%B")

    messages = [(
        f"🚚 **Hauler schedule: {label(files[0])}** · auto-updated <t:{ts}:R>\n"
        "-# Book trips in Google Calendar. This updates on its own.",
        [(files[0].name, files[0])],
    )]
    if len(files) > 1:
        names = " + ".join(label(f) for f in files[1:])
        messages.append((
            f"📅 **Coming up: {names}** · tap a picture to open it",
            [("SPOILER_" + f.name, f) for f in files[1:]],
        ))

    ids = _load_state().get("message_ids", [])
    if len(ids) == len(messages):
        ok = True
        for mid, (content, fl) in zip(ids, messages):
            r = requests.patch(f"{webhook}/messages/{mid}", files=_multipart(content, fl), timeout=60)
            if r.status_code != 200:
                print(f"Could not edit message {mid} ({r.status_code}): {r.text[:200]}")
                ok = False
                break
        if ok:
            print(f"Updated Discord messages {ids}")
            return

    # First run, or something got deleted: clear out leftovers and post fresh, in order.
    for mid in ids:
        requests.delete(f"{webhook}/messages/{mid}", timeout=30)
    new_ids = []
    for content, fl in messages:
        r = requests.post(f"{webhook}?wait=true", files=_multipart(content, fl), timeout=60)
        r.raise_for_status()
        new_ids.append(r.json()["id"])
    STATE_FILE.write_text(json.dumps({"message_ids": new_ids}, indent=2) + "\n")
    print(f"Posted new Discord messages {new_ids}")


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="only write PNGs to ./out, don't post")
    ap.add_argument("--ics", action="append", help="read a local .ics file instead of CALENDARS (repeatable)")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()

    tz = ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))
    months = max(1, min(6, int(os.environ.get("MONTHS", "3") or 3)))
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(tz).date()

    if args.ics:
        cals = [{"name": Path(p).stem.replace("_", " ").title(), "url": p,
                 "fill": PALETTE[i % len(PALETTE)][0], "text": PALETTE[i % len(PALETTE)][1]}
                for i, p in enumerate(args.ics)]
    else:
        cals = parse_calendars(os.environ.get("CALENDARS", ""))
    if not cals:
        sys.exit("No calendars configured. Set the CALENDARS secret (see README).")

    first = dt.date(today.year, today.month, 1)
    ly, lm = add_months(today.year, today.month, months)
    win_start = first - dt.timedelta(days=7)
    win_end = dt.date(ly, lm, 1) + dt.timedelta(days=14)

    trips: list[Trip] = []
    for i, c in enumerate(cals):
        if args.ics:
            text = Path(c["url"]).read_text(encoding="utf-8")
        else:
            r = requests.get(c["url"], timeout=30)
            if r.status_code != 200:
                sys.exit(f"Could not download calendar '{c['name']}' (HTTP {r.status_code}). "
                         "Check the secret iCal address in the CALENDARS secret.")
            text = r.content.decode("utf-8", errors="replace")
        found = parse_ics(text, i, tz, win_start, win_end)
        print(f"{c['name']}: {len(found)} trip(s) in range")
        trips += found

    OUT_DIR.mkdir(exist_ok=True)
    files = []
    for n in range(months):
        y, m = add_months(today.year, today.month, n)
        path = OUT_DIR / f"hauler_{y}_{m:02d}.png"
        render_month(y, m, trips, cals, today).save(path, optimize=True)
        files.append(path)
        print(f"Rendered {path.name}")

    if args.preview:
        return
    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        sys.exit("WEBHOOK_URL secret is missing (see README).")
    post_to_discord(webhook, files)


if __name__ == "__main__":
    main()
