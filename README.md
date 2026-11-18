# 🎓 MTI Academic Tracker

An automated, lightweight Python tool designed to help MTI University Computer Science students stay updated with their Moodle assignments and midterm grades. The bot continuously monitors the platform and broadcasts new deadlines and grade postings directly to Telegram.

## ✨ Features
- **Ultra-Fast Scraping:** Uses `requests` and `BeautifulSoup4` (replacing Selenium) to query Moodle's internal AJAX API and parse grade tables in seconds with minimal resource usage.
- **Assignments & Grades Tracking:** Monitors both upcoming Moodle events/deadlines and midterm grade announcements.
- **Smart Memory:** Maintains JSON records (`sent_events.json` and `sent_grades.json`) and saves IDs only after confirmed delivery to prevent spam or missed alerts.
- **Reliable Scheduling:** Runs serverless on GitHub Actions, triggered every 10 minutes via `cron-job.org` (`workflow_dispatch`) with an automated fallback cron to avoid queue delays.
- **Instant Broadcasts:** Sends cleanly formatted HTML updates with Cairo timezone (`Africa/Cairo`) timestamps directly to Telegram.

## 📖 Project Origins & Acknowledgements
The core concept of this tool was inspired by similar open-source academic scrapers. Recognizing the specific needs of our CS cohort, I adopted the baseline idea and completely re-engineered it to fit our MTI Moodle infrastructure—eventually migrating from headless browser automation to direct API requests for maximum speed and 99% reliability.

Throughout the development, optimization, and debugging lifecycle, I collaborated with an AI assistant as a thought partner to refactor the scraper, handle edge cases, and architect the automated GitHub Actions + external cron workflow.

## 🛠️ Tech Stack
- Python 3
- `requests` & `BeautifulSoup4`
- Telegram Bot API (HTML formatting)
- GitHub Actions, Secrets & `cron-job.org` for reliable serverless deployment

*Developed and maintained to streamline the academic workflow for MTI CS students.*
