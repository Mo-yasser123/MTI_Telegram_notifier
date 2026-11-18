import os
import sys
import json
import re
import html
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
UNI_USER = os.getenv("UNI_USER")
UNI_PASS = os.getenv("UNI_PASS")

BASE_URL = "https://elearning-01.mti.edu.eg"
EVENTS_FILE = os.path.join(BASE_DIR, "sent_events.json")
GRADES_FILE = os.path.join(BASE_DIR, "sent_grades.json")
CAIRO_TZ = ZoneInfo("Africa/Cairo")
MAX_SAVED = 500
REQUEST_TIMEOUT = 25


# ==========================================
# أدوات مساعدة
# ==========================================
def get_professor_name(course_name):
    name_lower = course_name.lower()
    if "english language" in name_lower:
        return "Dr. Rasha Saeed"
    elif "introduction to programming" in name_lower:
        return "Dr. HAFEZ M.S. ABDEL-WAHAB"
    elif "introduction to computer science" in name_lower:
        return "Dr. Mohamed M. Elgazzar"
    elif "linear algebra" in name_lower:
        return "Walid Basiony"
    elif "mathematical analysis" in name_lower:
        return "Dr. Rania Ahmed"
    elif "entrepreneur" in name_lower:
        return "Dr. El-sayed Orabi"
    return "Not Specified"


def load_json(filename):
    try:
        with open(filename, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data[-MAX_SAVED:], f, ensure_ascii=False, indent=4)


def build_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
    })
    retry_strategy = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ==========================================
# تسجيل الدخول والسحب من Moodle
# ==========================================
def login_moodle(session):
    login_url = f"{BASE_URL}/login/index.php"
    resp = session.get(login_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    token_match = re.search(r'name="logintoken"\s+value="([^"]+)"', resp.text)
    if not token_match:
        raise RuntimeError("Could not find logintoken on Moodle login page.")

    payload = {
        "username": UNI_USER,
        "password": UNI_PASS,
        "logintoken": token_match.group(1),
    }
    post_resp = session.post(login_url, data=payload, timeout=REQUEST_TIMEOUT)
    post_resp.raise_for_status()

    if "login/index.php" in post_resp.url:
        raise RuntimeError("Moodle login failed. Check UNI_USER and UNI_PASS.")

    dash_resp = session.get(f"{BASE_URL}/my/", timeout=REQUEST_TIMEOUT)
    dash_resp.raise_for_status()

    sesskey_match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', dash_resp.text)
    if not sesskey_match:
        raise RuntimeError("Could not extract sesskey from Moodle dashboard.")

    return dash_resp.text, sesskey_match.group(1)


def scrape_events(session, sesskey, saved_events):
    new_events = []
    ajax_url = (
        f"{BASE_URL}/lib/ajax/service.php"
        f"?sesskey={sesskey}&info=core_calendar_get_action_events_by_timesort"
    )
    ajax_payload = [{
        "index": 0,
        "methodname": "core_calendar_get_action_events_by_timesort",
        "args": {"limitnum": 20, "timesortfrom": 0},
    }]

    resp = session.post(ajax_url, json=ajax_payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or not data or data[0].get("error"):
        raise RuntimeError(f"Moodle AJAX error: {data}")

    events_list = data[0].get("data", {}).get("events", [])
    seen_now = set()

    for event in events_list:
        raw_title = (event.get("name") or "No Title").strip()
        event_link = event.get("url") or f"{BASE_URL}/my/"
        course_name = (event.get("course", {}).get("fullname") or "Unknown Course").strip()

        raw_desc = event.get("description") or ""
        description = BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True) if raw_desc else ""

        time_sort = event.get("timesort")
        if time_sort:
            date_time = datetime.fromtimestamp(time_sort, tz=CAIRO_TZ).strftime("%Y-%m-%d %I:%M %p")
        else:
            date_time = "Unknown Time"

        event_id = str(event.get("id") or f"{raw_title}_{date_time}")

        if event_id in saved_events or event_id in seen_now:
            continue
        seen_now.add(event_id)

        new_events.append({
            "id": event_id,
            "raw_title": raw_title,
            "date": date_time,
            "link": event_link,
            "course_name": course_name,
            "description": description,
        })

    return new_events


def scrape_grades(session, dash_html, saved_grades):
    new_grades = []
    soup = BeautifulSoup(dash_html, "html.parser")

    courses = {}
    for link in soup.select("a[href*='course/view.php?id=']"):
        href = link.get("href", "")
        name = link.get_text(" ", strip=True)
        match = re.search(r"id=(\d+)", href)
        if match and name:
            courses[match.group(1)] = name

    keywords = ["mid", "mt", "ميد"]

    for c_id, c_name in courses.items():
        grade_id = f"{c_id}_midterm"
        if grade_id in saved_grades:
            continue

        grade_url = f"{BASE_URL}/grade/report/user/index.php?id={c_id}"
        try:
            resp = session.get(grade_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            grade_soup = BeautifulSoup(resp.text, "html.parser")

            for row in grade_soup.find_all("tr"):
                row_text = row.get_text(" ", strip=True).lower()
                if any(kw in row_text for kw in keywords):
                    grade_cell = row.select_one("[class*='column-grade']")
                    if not grade_cell:
                        continue
                    grade_val = grade_cell.get_text(strip=True)
                    if grade_val and grade_val not in ("-", "—", ""):
                        new_grades.append({
                            "id": grade_id,
                            "course_name": c_name,
                            "link": grade_url,
                        })
                        break
        except Exception as e:
            print(f"Warning: Could not check grades for {c_name}: {e}")

    return new_grades


# ==========================================
# تليجرام
# ==========================================
def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=20)
        if response.status_code == 200 and response.json().get("ok"):
            print("Message sent successfully.")
            return True
        print(f"Telegram failed: {response.text}")
    except Exception as e:
        print(f"Telegram API error: {e}")
    return False


def build_event_message(event):
    raw_title = event["raw_title"]
    task_name = raw_title.replace(" is due", "").strip()
    course_name = event["course_name"]
    professor = get_professor_name(course_name)

    safe_course = html.escape(course_name)
    safe_prof = html.escape(professor)
    safe_task = html.escape(task_name)
    safe_date = html.escape(event["date"])
    safe_link = html.escape(event["link"], quote=True)

    message = "🚨 <b>New Academic Update</b>\n\n"
    message += f"📚 <b>Course:</b> <code>{safe_course}</code>\n"
    message += f"👨‍🏫 <b>Professor:</b> {safe_prof}\n"
    message += f"📝 <b>Task:</b> {safe_task}\n"
    message += f"⏳ <b>Deadline:</b> {safe_date}\n"

    if event.get("description"):
        desc = event["description"]
        if len(desc) > 120:
            desc = desc[:117] + "..."
        message += f"ℹ️ <b>Details:</b> <i>{html.escape(desc)}</i>\n"

    message += f"\n🔗 <a href='{safe_link}'><b>Click here to view details</b></a>"
    return message


def build_grade_message(grade):
    safe_course = html.escape(grade["course_name"])
    safe_link = html.escape(grade["link"], quote=True)

    message = "🎉 <b>منورين يا شباب...</b>\n\n"
    message += f"📊 <b>درجة امتحان</b> <code>{safe_course}</code> <b>نزلت.</b>\n\n"
    message += f"🔗 <a href='{safe_link}'><b>اضغط هنا لمعرفة النتيجة</b></a>"
    return message


# ==========================================
# التشغيل الرئيسي
# ==========================================
def main():
    print("Starting MTI Telegram Scraper (Requests + BS4)...")

    if not all([TELEGRAM_TOKEN, CHAT_ID, UNI_USER, UNI_PASS]):
        raise SystemExit("Missing env vars: TELEGRAM_TOKEN, CHAT_ID, UNI_USER, UNI_PASS")

    saved_events = load_json(EVENTS_FILE)
    saved_grades = load_json(GRADES_FILE)

    session = build_session()
    dash_html, sesskey = login_moodle(session)

    new_events = scrape_events(session, sesskey, saved_events)
    new_grades = scrape_grades(session, dash_html, saved_grades)

    if not new_events and not new_grades:
        print("No new events or grades found.")
        return

    for event in new_events:
        if send_to_telegram(build_event_message(event)):
            saved_events.append(event["id"])
            save_json(EVENTS_FILE, saved_events)

    for grade in new_grades:
        if send_to_telegram(build_grade_message(grade)):
            saved_grades.append(grade["id"])
            save_json(GRADES_FILE, saved_grades)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
