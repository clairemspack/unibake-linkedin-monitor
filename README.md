# Lantmännen Unibake — LinkedIn Activity Monitor

Pulls LinkedIn publishing data from Brandwatch, analyses it with Claude,
and produces a follow-up brief + HTML report. Two API calls, no database,
no server — runs on any machine with Python 3.10+.

---

## Setup (one-time, ~5 minutes)

**1. Install Python 3.10 or later**
Download from https://python.org if not already installed.

**2. Install dependencies**
Open a terminal in this folder and run:
```
pip install -r requirements.txt
```

**3. Add your API keys to config.json**
Open `config.json` and fill in:
- `brandwatch_api_key` — from Brandwatch → Settings → API Access
- `anthropic_api_key` — from https://console.anthropic.com → API Keys

---

## Running it

**Basic run** (saves HTML report, prints brief to terminal):
```
python monitor.py
```

**Preview with sample data** (no API calls needed — good for testing):
```
python monitor.py --dry-run
```

**Run and send email** (requires email config in config.json):
```
python monitor.py --email
```

---

## What it produces

Each run saves a file called `report_YYYY-MM-DD.html` in the same folder.
Open it in any browser — it contains:
- Summary stats (active / low / silent markets)
- Full market table with post counts, engagement, last post date
- Claude-drafted follow-up email targeting inactive markets

The follow-up brief is also printed directly to the terminal so you can
copy it to Outlook without opening the report.

---

## Scheduling (bi-weekly, automatic)

### Windows — Task Scheduler

1. Open Task Scheduler → Create Basic Task
2. Name it "Unibake LinkedIn Monitor"
3. Trigger: Weekly, repeat every 2 weeks, Monday at 07:00
4. Action: Start a program
   - Program: `python`
   - Arguments: `C:\path\to\monitor.py`
   - Start in: `C:\path\to\this\folder`
5. Finish

To also send the email automatically, change the Arguments line to:
`C:\path\to\monitor.py --email`

### macOS/Linux — cron

Add this to your crontab (`crontab -e`):
```
0 7 * * 1 cd /path/to/this/folder && python monitor.py --email >> monitor.log 2>&1
```

This runs every Monday at 07:00. For every other Monday only, use:
```
0 7 */14 * * cd /path/to/this/folder && python monitor.py --email >> monitor.log 2>&1
```

---

## Email setup (optional)

If you want the report emailed automatically, fill in the `email` section
of config.json:

```json
"email": {
  "enabled": true,
  "smtp_server": "smtp.office365.com",
  "smtp_port": 587,
  "sender": "claire@lantmannen.com",
  "password": "your-password",
  "recipients": ["claire@lantmannen.com", "debora@lantmannen.com"]
}
```

For Office 365, use an app password rather than your main account password
if MFA is enabled — generate one at https://mysignins.microsoft.com/security-info

---

## Adjusting thresholds

In config.json:
- `active_threshold`: posts needed to be "Active" (default: 3)
- `low_threshold`: minimum posts for "Low" vs "Silent" (default: 1)
- `lookback_days`: how far back to look (default: 14)

---

## Troubleshooting

**"No LinkedIn channels found"**
Check that your LinkedIn company pages are connected as owned channels
in Brandwatch (Settings → Social Accounts).

**"Invalid Brandwatch API key (401)"**
Regenerate the key in Brandwatch → Settings → API Access.

**"ModuleNotFoundError"**
Run `pip install -r requirements.txt` again.

**Report generates but brief is empty**
Check your Anthropic API key in config.json and verify it has credit at
https://console.anthropic.com
