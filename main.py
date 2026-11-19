"""
MTI E-Learning Notifier
~~~~~~~~~~~~~~~~~~~~~~~
Monitors the MTI Moodle platform for new assignments and midterm grades,
then delivers formatted notifications to a Telegram chat.

Notifications sent:
  1. Immediately when a new assignment / deadline appears.
  2. Again as a reminder when the deadline is within REMINDER_HOURS hours.

Runs on GitHub Actions (triggered by cron-job.org or the built-in fallback
schedule). State is persisted as JSON files committed back to the repository.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID: str        = os.getenv("CHAT_ID", "")
UNI_USER: str       = os.getenv("UNI_USER", "")
UNI_PASS: str       = os.getenv("UNI_PASS", "")

MOODLE_BASE_URL   = "https://elearning-01.mti.edu.eg"
EVENTS_FILE       = BASE_DIR / "sent_events.json"
GRADES_FILE       = BASE_DIR / "sent_grades.json"
REMINDERS_FILE    = BASE_DIR / "sent_reminders.json"
CAIRO_TZ          = ZoneInfo("Africa/Cairo")
MAX_PERSISTED_IDS = 500
REQUEST_TIMEOUT   = 25   # seconds
REMINDER_HOURS    = 6    # send a reminder this many hours before the deadline

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mti_notifier")

# ---------------------------------------------------------------------------
# Course -> Instructor mapping
# ---------------------------------------------------------------------------

_INSTRUCTORS: list[tuple[str, str]] = [
    ("english language",                 "Dr. Rasha Saeed"),
    ("introduction to programming",      "Dr. Hafez M.S. Abdel-Wahab"),
    ("introduction to computer science", "Dr. Mohamed M. Elgazzar"),
    ("linear algebra",                   "Walid Basiony"),
    ("mathematical analysis",            "Dr. Rania Ahmed"),
    ("entrepreneur",                     "Dr. El-Sayed Orabi"),
]


def resolve_instructor(course: str) -> str:
    lowered = course.lower()
    for keyword, name in _INSTRUCTORS:
        if keyword in lowered:
            return name
    return "N/A"


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------


def load_ids(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_ids(path: Path, ids: list[str]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(ids[-MAX_PERSISTED_IDS:], fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


# ---------------------------------------------------------------------------
# Moodle authentication
# ---------------------------------------------------------------------------


def authenticate(session: requests.Session) -> tuple[str, str]:
    """
    Log in to Moodle and return (dashboard_html, sesskey).
    Raises RuntimeError on failure.
    """
    login_url = f"{MOODLE_BASE_URL}/login/index.php"

    page = session.get(login_url, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()

    token = re.search(r'name="logintoken"\s+value="([^"]+)"', page.text)
    if not token:
        raise RuntimeError("Login token not found on Moodle login page.")

    resp = session.post(
        login_url,
        data={"username": UNI_USER, "password": UNI_PASS,
              "logintoken": token.group(1)},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    if "login/index.php" in resp.url:
        raise RuntimeError("Moodle authentication failed -- check UNI_USER / UNI_PASS.")

    dashboard = session.get(f"{MOODLE_BASE_URL}/my/", timeout=REQUEST_TIMEOUT)
    dashboard.raise_for_status()

    sesskey = re.search(r'"sesskey"\s*:\s*"([^"]+)"', dashboard.text)
    if not sesskey:
        raise RuntimeError("Session key not found on Moodle dashboard.")

    log.info("Authenticated with Moodle successfully.")
    return dashboard.text, sesskey.group(1)


# ---------------------------------------------------------------------------
# Event scraping
# ---------------------------------------------------------------------------


def fetch_all_moodle_events(
    session: requests.Session,
    sesskey: str,
) -> list[dict[str, Any]]:
    """
    Fetch all upcoming events from the Moodle calendar AJAX API.
    Returns enriched dicts that include the raw Unix timestamp (timesort)
    so the reminder logic can compare it against the current time.
    """
    url = (
        f"{MOODLE_BASE_URL}/lib/ajax/service.php"
        f"?sesskey={sesskey}&info=core_calendar_get_action_events_by_timesort"
    )
    payload: list[dict[str, Any]] = [{
        "index": 0,
        "methodname": "core_calendar_get_action_events_by_timesort",
        "args": {"limitnum": 26, "timesortfrom": 0},
    }]

    resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or not data or data[0].get("error"):
        raise RuntimeError(f"Moodle AJAX error: {data}")

    results: list[dict[str, Any]] = []

    for ev in data[0].get("data", {}).get("events", []):
        title  = (ev.get("name") or "Untitled").strip()
        link   = ev.get("url") or f"{MOODLE_BASE_URL}/my/"
        course = (ev.get("course", {}).get("fullname") or "Unknown Course").strip()

        raw_desc    = ev.get("description") or ""
        description = (
            BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)
            if raw_desc else ""
        )

        ts       = ev.get("timesort")
        deadline = (
            datetime.fromtimestamp(ts, tz=CAIRO_TZ).strftime("%A, %d %b %Y  %I:%M %p")
            if ts else "Unknown"
        )

        event_id = str(ev.get("id") or f"{title}_{deadline}")

        results.append({
            "id":          event_id,
            "title":       title,
            "deadline":    deadline,
            "timesort":    ts,
            "link":        link,
            "course":      course,
            "description": description,
        })

    return results


def filter_new_events(
    all_events: list[dict[str, Any]],
    sent_ids: list[str],
) -> list[dict[str, Any]]:
    """Return events that have not been notified yet."""
    seen = set(sent_ids)
    new  = [ev for ev in all_events if ev["id"] not in seen]
    log.info("Found %d new event(s).", len(new))
    return new


def filter_reminder_events(
    all_events: list[dict[str, Any]],
    sent_reminder_ids: list[str],
) -> list[dict[str, Any]]:
    """
    Return events whose deadline is within the next REMINDER_HOURS hours
    and for which a reminder has not been sent yet.
    """
    now_ts           = datetime.now(tz=CAIRO_TZ).timestamp()
    cutoff_ts        = now_ts + REMINDER_HOURS * 3600
    already_reminded = set(sent_reminder_ids)

    due_soon = [
        ev for ev in all_events
        if ev["timesort"]
        and now_ts < ev["timesort"] <= cutoff_ts
        and ev["id"] not in already_reminded
    ]
    log.info("Found %d event(s) due within %d hours.", len(due_soon), REMINDER_HOURS)
    return due_soon


# ---------------------------------------------------------------------------
# Grade scraping
# ---------------------------------------------------------------------------

_MIDTERM_KEYWORDS = ["mid", "mt", "\u0645\u064a\u062f"]


def fetch_grades(
    session: requests.Session,
    dashboard_html: str,
    sent_ids: list[str],
) -> list[dict[str, str]]:
    """Return courses with newly published midterm grades."""

    soup    = BeautifulSoup(dashboard_html, "html.parser")
    courses = {}

    for a in soup.select("a[href*='course/view.php?id=']"):
        m    = re.search(r"id=(\d+)", a.get("href", ""))
        name = a.get_text(" ", strip=True)
        if m and name:
            courses[m.group(1)] = name

    sent_set = set(sent_ids)
    results  = []

    for cid, cname in courses.items():
        gid = f"{cid}_midterm"
        if gid in sent_set:
            continue

        grade_url = f"{MOODLE_BASE_URL}/grade/report/user/index.php?id={cid}"
        try:
            r = session.get(grade_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            gsoup = BeautifulSoup(r.text, "html.parser")

            for row in gsoup.find_all("tr"):
                text = row.get_text(" ", strip=True).lower()
                if not any(kw in text for kw in _MIDTERM_KEYWORDS):
                    continue
                cell = row.select_one("[class*='column-grade']")
                if not cell:
                    continue
                val = cell.get_text(strip=True)
                if val and val not in ("-", "\u2014", ""):
                    results.append({"id": gid, "course": cname, "link": grade_url})
                    break
        except Exception:
            log.warning("Could not retrieve grades for '%s'.", cname, exc_info=True)

    log.info("Found %d new grade(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_message(text: str) -> bool:
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            endpoint,
            json={"chat_id": CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Message delivered to Telegram.")
            return True
        log.error("Telegram rejected the message: %s", r.text)
    except Exception:
        log.error("Telegram request failed.", exc_info=True)
    return False


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

_DIVIDER = "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"


def build_event_message(event: dict[str, Any]) -> str:
    """Initial notification -- sent as soon as a new event appears."""
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape

    parts = [
        "<b>ACADEMIC NOTIFICATION</b>",
        _DIVIDER,
        f"<b>Course</b>      {e(event['course'])}",
        f"<b>Instructor</b>  {e(instructor)}",
        f"<b>Task</b>        {e(task)}",
        f"<b>Deadline</b>    {e(event['deadline'])}",
    ]

    desc = event.get("description", "")
    if desc:
        if len(desc) > 150:
            desc = desc[:147] + "..."
        parts.append(f"<b>Details</b>     <i>{e(desc)}</i>")

    parts += [
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_reminder_message(event: dict[str, Any]) -> str:
    """6-hour reminder -- distinct header so it is immediately recognisable."""
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape

    parts = [
        f"<b>DEADLINE REMINDER  --  {REMINDER_HOURS} HOURS LEFT</b>",
        _DIVIDER,
        f"<b>Course</b>      {e(event['course'])}",
        f"<b>Instructor</b>  {e(instructor)}",
        f"<b>Task</b>        {e(task)}",
        f"<b>Deadline</b>    {e(event['deadline'])}",
        "",
        "\u0627\u0644\u0645\u0647\u0645\u0629 \u062f\u064a \u0647\u062a\u0646\u062a\u0647\u064a \u062e\u0644\u0627\u0644 6 \u0633\u0627\u0639\u0627\u062a \u2014 \u0627\u062a\u0623\u0643\u062f \u0625\u0646\u0643 \u0633\u0644\u0645\u062a.",
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_grade_message(grade: dict[str, str]) -> str:
    e = html.escape
    parts = [
        "<b>GRADE PUBLISHED</b>",
        _DIVIDER,
        f"\u062f\u0631\u062c\u0629 \u0627\u0644\u0645\u064a\u062f \u062a\u064a\u0631\u0645 \u0628\u062a\u0627\u0639\u062a <b>{e(grade['course'])}</b> \u0646\u0632\u0644\u062a.",
        "",
        f"<a href='{e(grade['link'], quote=True)}'><b>\u0627\u0639\u0631\u0641 \u0646\u062a\u064a\u062c\u062a\u0643</b></a>",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("MTI Notifier -- starting.")

    missing = [k for k, v in [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("CHAT_ID", CHAT_ID),
        ("UNI_USER", UNI_USER),             ("UNI_PASS", UNI_PASS),
    ] if not v]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    sent_events    = load_ids(EVENTS_FILE)
    sent_grades    = load_ids(GRADES_FILE)
    sent_reminders = load_ids(REMINDERS_FILE)

    session        = build_session()
    dash_html, key = authenticate(session)

    all_events = fetch_all_moodle_events(session, key)
    new_events = filter_new_events(all_events, sent_events)
    due_soon   = filter_reminder_events(all_events, sent_reminders)
    new_grades = fetch_grades(session, dash_html, sent_grades)

    if not new_events and not due_soon and not new_grades:
        log.info("Nothing new -- exiting.")
        return

    for ev in new_events:
        if send_message(build_event_message(ev)):
            sent_events.append(ev["id"])
            save_ids(EVENTS_FILE, sent_events)

    for ev in due_soon:
        if send_message(build_reminder_message(ev)):
            sent_reminders.append(ev["id"])
            save_ids(REMINDERS_FILE, sent_reminders)

    for gr in new_grades:
        if send_message(build_grade_message(gr)):
            sent_grades.append(gr["id"])
            save_ids(GRADES_FILE, sent_grades)

    log.info(
        "Done -- %d new event(s), %d reminder(s), %d grade(s) sent.",
        len(new_events), len(due_soon), len(new_grades),
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
