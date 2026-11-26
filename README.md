# Hauler Calendar → Discord

Shows your Google Calendar car-hauler trips inside Discord, with multi-day trips drawn as solid bars across the days.

- **Message 1:** this month, shown big.
- **Message 2:** the next two months, blurred until someone taps them.
- Updates itself every hour. You only ever book trips in Google Calendar.
- Costs nothing. It runs on GitHub's free scheduled jobs and posts through a Discord webhook (no bot to host).

---

## One-time setup (about 10 minutes)

### 1. Google Calendar: get the private feed address
Do this on a computer, at calendar.google.com.

1. On the left, under **My calendars**, hover your hauler calendar (e.g. "Hauler 1") → **⋮** → **Settings and sharing**.
2. Scroll to **Integrate calendar**.
3. Copy **Secret address in iCal format** (it ends in `basic.ics`).

> Treat that address like a password: anyone who has it can read the calendar. It only goes into a GitHub *secret* (step 3), where nobody can see it.

### 2. Discord: create the webhook
1. Right-click `#hauler-schedule` → **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
2. Name it `Hauler Schedule`, then click **Copy Webhook URL**.

### 3. GitHub: add the two secrets
In this repo: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

| Name | Value |
|---|---|
| `CALENDARS` | `Hauler 1 \| <secret iCal address from step 1>` |
| `WEBHOOK_URL` | the webhook URL from step 2 |

Got more than one truck? Put each one on its own line in `CALENDARS`. You can also add a color:
```
Hauler 1 | https://calendar.google.com/calendar/ical/.../basic.ics
Hauler 2 | https://calendar.google.com/calendar/ical/.../basic.ics | #8E24AA
```

### 4. Run it the first time
**Actions** tab → **Update hauler calendar** → **Run workflow**. After about a minute, both messages appear in Discord.

### 5. Pin it
In Discord, pin the first message. If you set `#hauler-schedule` so only admins can send messages, the calendar always stays at the bottom where people look.

---

## Day to day
- **Book a trip:** in Google Calendar, check **All day** and set the start and end dates to cover the whole run, including the drive home. That's what makes it show as a solid block.
- **Need it updated right now?** Actions → Run workflow. Otherwise it updates within the hour.
- **Want 1, 2 or up to 6 months?** Settings → Secrets and variables → Actions → **Variables** tab → add `MONTHS` = `2` (or whatever you want). This month always shows big, and the rest go into the tap-to-open message.

## Good to know
- Keep this repo **private**. Running hourly uses about 720 of the 2,000 free GitHub Actions minutes a private account gets each month. If your other project uses a lot, change `17 * * * *` in `.github/workflows/update-calendar.yml` to `17 */2 * * *` to run every 2 hours.
- `state.json` just stores the IDs of the two Discord messages so they get edited instead of reposted. If you delete them in Discord, the next run posts fresh ones on its own.
- Header shows **days booked per truck** each month. That's your utilization number.

## Preview locally (optional)
```
pip install -r requirements.txt
python hauler_calendar.py --preview --ics my_export.ics
```
The images are saved to `out/`.
