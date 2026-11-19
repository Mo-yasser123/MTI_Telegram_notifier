"""
MTI E-Learning Notifier
~~~~~~~~~~~~~~~~~~~~~~~
Monitors the MTI Moodle platform for open assignments and midterm grades,
then delivers formatted notifications to a Telegram chat.

Notifications sent:
1. Immediately when a new assignment is open for submission (or a new quiz appears).
2. First reminder when the deadline is within REMINDER_HOURS hours.
3. Final reminder when the deadline is within FINAL_REMINDER_HOURS hours.

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

MOODLE_BASE_URL      = "https://elearning-01.mti.edu.eg"
EVENTS_FILE          = BASE_DIR / "sent_events.json"
GRADES_FILE          = BASE_DIR / "sent_grades.json"
REMINDERS_FILE       = BASE_DIR / "sent_reminders.json"
CAIRO_TZ             = ZoneInfo("Africa/Cairo")
MAX_PERSISTED_IDS    = 500
REQUEST_TIMEOUT      = 25  # seconds
REMINDER_HOURS       = 6   # first reminder before the deadline
FINAL_REMINDER_HOURS = 2   # final reminder before the deadline

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
        data={
            "username": UNI_USER,
            "password": UNI_PASS,
            "logintoken": token.group(1),
        },
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
# Event scraping & submission status check
# ---------------------------------------------------------------------------


def is_submission_open(session: requests.Session, url: str) -> bool:
    """
    Check if a Moodle assignment has the 'Add submission' button enabled.
    Non-assignment events (e.g. quizzes) pass through immediately.
    """
    if "/mod/assign/" not in url:
        return True

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.select_one("input[name='action'][value='editsubmission']") is not None
    except Exception:
        log.warning("Could not verify submission status for %s", url, exc_info=True)
        return False


def fetch_all_moodle_events(
    session: requests.Session,
    sesskey: str,
) -> list[dict[str, Any]]:
    """
    Fetch upcoming events from the Moodle calendar AJAX API
    starting from the current timestamp (ignores expired tasks).
    """
    now_ts = int(datetime.now(tz=CAIRO_TZ).timestamp())
    url = (
        f"{MOODLE_BASE_URL}/lib/ajax/service.php"
        f"?sesskey={sesskey}&info=core_calendar_get_action_events_by_timesort"
    )
    payload: list[dict[str, Any]] = [{
        "index": 0,
        "methodname": "core_calendar_get_action_events_by_timesort",
        "args": {"limitnum": 26, "timesortfrom": now_ts},
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
    session: requests.Session,
    all_events: list[dict[str, Any]],
    sent_ids: list[str],
) -> list[dict[str, Any]]:
    """Return events that have not been notified yet and are open for submission."""
    seen = set(sent_ids)
    new: list[dict[str, Any]] = []

    for ev in all_events:
        if ev["id"] in seen:
            continue
        if not is_submission_open(session, ev["link"]):
            log.info("Skipping locked assignment: %s", ev["title"])
            continue
        new.append(ev)

    log.info("Found %d new open event(s).", len(new))
    return new


def filter_reminder_events(
    all_events: list[dict[str, Any]],
    sent_event_ids: list[str],
    sent_reminder_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Return (first_reminders, final_reminders) for already-notified open events
    approaching their deadline.
    """
    now_ts           = datetime.now(tz=CAIRO_TZ).timestamp()
    first_cutoff_ts  = now_ts + REMINDER_HOURS * 3600
    final_cutoff_ts  = now_ts + FINAL_REMINDER_HOURS * 3600
    known_open_ids   = set(sent_event_ids)
    already_reminded = set(sent_reminder_ids)

    first_reminders: list[dict[str, Any]] = []
    final_reminders: list[dict[str, Any]] = []

    for ev in all_events:
        ts = ev.get("timesort")
        if not ts or not (now_ts < ts <= first_cutoff_ts):
            continue
        # Only remind for events that were already opened and notified in a prior run.
        if ev["id"] not in known_open_ids:
            continue

        final_key = f"{ev['id']}_final"
        first_key = f"{ev['id']}_{REMINDER_HOURS}h"

        if ts <= final_cutoff_ts:
            if final_key not in already_reminded:
                final_reminders.append({**ev, "reminder_key": final_key})
        else:
            if first_key not in already_reminded and ev["id"] not in already_reminded:
                first_reminders.append({**ev, "reminder_key": first_key})

    log.info(
        "Found %d first reminder(s) and %d final reminder(s).",
        len(first_reminders),
        len(final_reminders),
    )
    return first_reminders, final_reminders


# ---------------------------------------------------------------------------
# Grade scraping
# ---------------------------------------------------------------------------

_MIDTERM_KEYWORDS = ["mid", "mt", "ميد"]


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
                if val and val not in ("-", "—", ""):
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
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
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

_DIVIDER = "────────────────────"


def build_event_message(event: dict[str, Any]) -> str:
    """Initial notification -- sent as soon as a new open event appears."""
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
    """First reminder -- 6 hours before the deadline."""
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
        f"المهمة دي هتنتهي خلال {REMINDER_HOURS} ساعات — اتأكد إنك سلمت.",
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_final_reminder_message(event: dict[str, Any]) -> str:
    """Final reminder -- right before the deadline closes."""
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape

    parts = [
        f"<b>FINAL REMINDER  --  LESS THAN {FINAL_REMINDER_HOURS} HOURS LEFT</b>",
        _DIVIDER,
        f"<b>Course</b>      {e(event['course'])}",
        f"<b>Instructor</b>  {e(instructor)}",
        f"<b>Task</b>        {e(task)}",
        f"<b>Deadline</b>    {e(event['deadline'])}",
        "",
        "تنبيه أخير: الموعد قرب يقفل — سلّم دلوقتي قبل ما الوقت يخلص.",
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_grade_message(grade: dict[str, str]) -> str:
    e = html.escape
    parts = [
        "<b>GRADE PUBLISHED</b>",
        _DIVIDER,
        f"درجة الميد تيرم بتاعت <b>{e(grade['course'])}</b> نزلت.",
        "",
        f"<a href='{e(grade['link'], quote=True)}'><b>اعرف نتيجتك</b></a>",
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
    new_events = filter_new_events(session, all_events, sent_events)

    first_reminders, final_reminders = filter_reminder_events(
        all_events, sent_events, sent_reminders
    )
    new_grades = fetch_grades(session, dash_html, sent_grades)

    if not new_events and not first_reminders and not final_reminders and not new_grades:
        log.info("Nothing new -- exiting.")
        return

    for ev in new_events:
        if send_message(build_event_message(ev)):
            sent_events.append(ev["id"])
            save_ids(EVENTS_FILE, sent_events)

    for ev in first_reminders:
        if send_message(build_reminder_message(ev)):
            sent_reminders.append(ev["reminder_key"])
            save_ids(REMINDERS_FILE, sent_reminders)

    for ev in final_reminders:
        if send_message(build_final_reminder_message(ev)):
            sent_reminders.append(ev["reminder_key"])
            save_ids(REMINDERS_FILE, sent_reminders)

    for gr in new_grades:
        if send_message(build_grade_message(gr)):
            sent_grades.append(gr["id"])
            save_ids(GRADES_FILE, sent_grades)

    log.info(
        "Done -- %d new event(s), %d first reminder(s), %d final reminder(s), %d grade(s) sent.",
        len(new_events), len(first_reminders), len(final_reminders), len(new_grades),
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
