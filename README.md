# MTI E-Learning Telegram Notifier

Automated Python bot that monitors the MTI University Moodle portal and delivers real-time academic alerts, schedule updates, and midterm grade notifications to Telegram.

---

## Key Features

| Feature | Details |
|---|---|
| Verified Event Tracking | Pulls event details from Moodle's Calendar AJAX API and cross-checks them against the Dashboard **Upcoming events** block so only active, visible items are sent |
| Schedule Change Detection | Tracks event timestamps (`timesort`) to detect rescheduled deadlines or quiz times, immediately sends an update alert, and resets old reminder keys |
| Two-Stage Smart Reminders | Sends a 6-hour reminder and a final alert within 1 hour, distinguishing between **quiz openings**, **quiz closings**, and **regular tasks** |
| Exact Time Remaining | Calculates and displays the actual remaining time in hours and minutes at the moment of delivery to avoid confusion |
| Midterm Grade Alerts | Monitors enrolled course grade reports and notifies as soon as midterm grades are published |
| Manual Test Channel Dispatch | Supports manual workflow triggering with a dedicated `test_channel` input to safely verify message formatting and delivery before broadcasting to the main channel |
| Clean Functional Formatting | Uses minimal, purposeful emojis and structured HTML templates suitable for university communities |
| Persistent State Memory | Maintains JSON state files for event timestamps, reminder keys, and grade IDs to prevent duplicates |
| Automated Execution | Runs serverless on GitHub Actions every 30 minutes with concurrency control and automated state commits |

---

## Architecture & Workflow

```text
GitHub Actions (Every 30m / Manual) ──▶ Moodle (Auth + Dashboard & AJAX API) ──▶ Telegram Bot API
       │                                                                               │
 [test_channel input]                                                       (Main or Test Channel)
                                               │
                                         State Sync
                                     (sent_events.json
                                      sent_reminders.json
                                      sent_grades.json)
```

1. **Trigger & Routing:** GitHub Actions executes `.github/workflows/scraper.yml` on a 30-minute schedule (`*/30 * * * *`) or via manual dispatch (`workflow_dispatch`) with an optional `test_channel` input to route alerts to a sandbox Telegram channel.
2. **Authentication & Verification:** `main.py` authenticates using encrypted repository secrets, extracts session tokens (`logintoken` and `sesskey`), and validates Calendar API events against the dashboard's `Upcoming events` block.
3. **Change & Reminder Engine:** Compares live events against stored timestamps in `sent_events.json` to separate brand-new items from rescheduled ones, then evaluates 6-hour and 1-hour reminder windows in `Africa/Cairo` time.
4. **Grade Monitoring:** Checks course grade tables for newly published midterm scores not yet logged in `sent_grades.json`.
5. **Delivery & Persistence:** Sends formatted HTML messages to the target Telegram channel and commits updated JSON state files back to the repository.

---

## Project Structure

```text
MTI_Telegram_notifier/
├── .github/
│   └── workflows/
│       └── scraper.yml       # Scheduled & manual GitHub Actions workflow (with test_channel support)
├── .gitignore
├── README.md
├── main.py                   # Core authentication, scraping, filtering, and Telegram delivery
├── requirements.txt          # Python dependencies
├── sent_events.json          # Persisted event ID -> timestamp map (auto-managed)
├── sent_grades.json          # Persisted midterm grade IDs (auto-managed)
└── sent_reminders.json       # Persisted 6h & final reminder keys (auto-managed)
```

---

## Sample Telegram Messages

### 📌 New Event
```text
📌 ACADEMIC NOTIFICATION
────────────────────
Course      Introduction to Programming
Instructor  Dr. Hafez M.S. Abdel-Wahab
Task        Lab 5 Submission
Deadline    Monday, 25 Nov 2026  11:59 PM
────────────────────
Open in Moodle
```

### 🔄 Schedule Updated
```text
🔄 SCHEDULE UPDATED
────────────────────
Course      F26 English Language I
Instructor  Dr. Rasha Saeed
Task        Quiz 1 closes
Old Time    Friday, 20 Nov 2026  09:00 PM
New Time    Saturday, 21 Nov 2026  09:00 PM

تم تعديل الموعد على المنصة — راجع الميعاد الجديد.
The schedule/deadline was updated on Moodle — check the new time.
────────────────────
Open in Moodle
```

### ⏳ 6-Hour Reminder
```text
⏳ DEADLINE REMINDER  --  6 HOURS LEFT
────────────────────
Course      F26 English Language I
Instructor  Dr. Rasha Saeed
Task        Quiz 1 closes
Deadline    Saturday, 21 Nov 2026  09:00 PM
Left Now    5h 45m (5 ساعات و45 دقيقة)

الحق بسرعة يا شباب الكويز قرب يقفل — فاضل 6 ساعات بس.
Hurry up! The quiz is closing soon — only 6 hours left.
────────────────────
Open in Moodle
```

### 🚨 Final Reminder (Less Than 1 Hour)
```text
🚨 FINAL REMINDER  --  LESS THAN 1 HOUR LEFT
────────────────────
Course      Introduction to Programming
Instructor  Dr. Hafez M.S. Abdel-Wahab
Task        Lab 5 Submission
Deadline    Monday, 25 Nov 2026  11:59 PM
Left Now    42m (42 دقيقة)

تنبيه أخير: الموعد قرب يقفل — سلّم دلوقتي قبل ما الوقت يخلص.
Final reminder: the deadline is closing soon — submit now before time runs out.
────────────────────
Open in Moodle
```

### 🎓 Grade Published
```text
🎓 GRADE PUBLISHED
────────────────────
درجة الميد تيرم بتاعت Introduction to Programming نزلت.

اعرف نتيجتك
```

---

## Tech Stack

- **Latest Python 3 (`Python 3.12+`)**
- **`requests` & `BeautifulSoup4`** — Session management, HTML parsing, and Moodle AJAX API integration
- **`python-dotenv` & `zoneinfo`** — Environment configuration and `Africa/Cairo` timezone handling
- **Telegram Bot API** — Structured HTML message delivery
- **GitHub Actions** — Scheduled execution, manual test dispatch, and automated state persistence

---

### Bot currently status : 🟢
---

## Copyright & License

All rights reserved © **Mohamed Yasser**.
