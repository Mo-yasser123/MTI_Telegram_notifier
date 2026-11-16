# 🎓 MTI Academic Tracker

An automated Python-based scraping tool designed to help MTI University Computer Science students stay updated with their Moodle assignments. The bot quietly monitors the platform and broadcasts new deadlines directly to our student community.

## ✨ Features
- **Automated Scraping:** Uses Selenium to securely log in and check for new Moodle events.
- **Smart Memory:** Maintains a JSON record of sent events to prevent spam and duplicate notifications.
- **CI/CD Integration:** Runs continuously and serverless on GitHub Actions via scheduled cron jobs.
- **Instant Broadcasts:** Sends formatted updates (Embeds/HTML) directly to students.

## 📖 Project Origins & Acknowledgements
The core concept of this tool was inspired by similar open-source academic scrapers. Recognizing the specific needs of our CS cohort, I adopted the baseline idea and completely re-engineered it to fit our MTI Moodle infrastructure. 

Throughout the development and debugging lifecycle, I collaborated with an AI assistant as a thought partner to refine the code, handle encoding issues, and architect the automated GitHub Actions workflow.

## 🛠️ Tech Stack
- Python 3
- Selenium WebDriver
- API Integrations (discord.py / requests)
- GitHub Actions & Secrets for secure deployment

*Developed and maintained to streamline the academic workflow for MTI CS students.*
