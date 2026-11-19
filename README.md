# MTI E-Learning Telegram Notifier

Automated bot that monitors the MTI University Moodle portal and delivers real-time academic alerts to Telegram — no manual checking required.

---

## What It Does

| Feature | Details |
|---|---|
| Assignment Tracking | Fetches upcoming deadlines directly from Moodle's calendar API |
| Grade Alerts | Detects the moment midterm grades are published |
| Deadline Reminders | Sends a second alert 6 hours before each deadline |
| Telegram Delivery | Sends clean, structured HTML messages to any chat or group |
| Persistent Memory | Stores sent IDs in JSON — no duplicate notifications |
| Fully Automated | Runs serverless on GitHub Actions, triggered externally via cron-job.org |

---

## How It Works

```
cron-job.org  --▶  GitHub Actions  --▶  Moodle (login + scrape)  --▶  Telegram
                         |
                    Commit state
                  (sent_events.json
                   sent_grades.json
                   sent_reminders.json)
```

1. `cron-job.org` hits the `workflow_dispatch` webhook every 10 minutes.
2. GitHub Actions authenticates with Moodle using stored secrets.
3. New events and grades are compared against the persisted ID lists.
4. Any new item is formatted and sent to Telegram.
5. Events within 6 hours of their deadline trigger a reminder notification.
6. Updated state files are committed back to the repository.

---

## Setup

### 1. Fork or clone this repository

```
git clone https://github.com/your-username/MTI_Telegram_notifier.git
```

### 2. Create a Telegram bot

1. Open Telegram, search for `@BotFather`, send `/newbot`.
2. Save the bot token you receive.
3. Send any message to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` value.

### 3. Add GitHub Secrets

Go to Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `TELEGRAM_TOKEN` | Your Telegram bot token |
| `CHAT_ID` | Your Telegram chat/group ID |
| `UNI_USER` | Your MTI Moodle username |
| `UNI_PASS` | Your MTI Moodle password |

---

## Project Structure

```
MTI_Telegram_notifier/
├── main.py                   # Core scraper and notifier
├── requirements.txt          # Python dependencies
├── sent_events.json          # Persisted event IDs  (auto-managed)
├── sent_grades.json          # Persisted grade IDs  (auto-managed)
├── sent_reminders.json       # Persisted reminder IDs (auto-managed)
├── .env.example              # Environment variable template
├── .gitignore
└── .github/
    └── workflows/
        └── scraper.yml       # GitHub Actions workflow
```

---

## Sample Telegram Messages

Assignment alert:
```
ACADEMIC NOTIFICATION
────────────────────
Course      Introduction to Programming
Instructor  Dr. Hafez M.S. Abdel-Wahab
Task        Lab 5 Submission
Deadline    Monday, 25 Nov 2026  11:59 PM
────────────────────
Open in Moodle
```

6-hour reminder:
```
DEADLINE REMINDER  --  6 HOURS LEFT
────────────────────
Course      Introduction to Programming
Instructor  Dr. Hafez M.S. Abdel-Wahab
Task        Lab 5 Submission
Deadline    Monday, 25 Nov 2026  11:59 PM

المهمة دي هتنتهي خلال 6 ساعات — اتأكد إنك سلمت.
────────────────────
Open in Moodle
```

Grade alert:
```
GRADE PUBLISHED
────────────────────
درجة الميد تيرم بتاعت Introduction to Programming نزلت.

اعرف نتيجتك
```

---

## Tech Stack

- Python 3.12
- `requests` + `BeautifulSoup4` — fast, headless scraping via Moodle's internal AJAX API
- Telegram Bot API — HTML-formatted message delivery
- GitHub Actions — serverless scheduled execution
- cron-job.org — reliable external trigger

---

## Development Notes

Built with AI-assisted vibe coding — the architecture, scraping logic, and workflow were developed collaboratively with an AI assistant, then refined through iterative testing against the live Moodle environment.

---

## License

For personal and educational use. Fork freely.
