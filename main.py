
"""
MTI E-Learning Notifier
~~~~~~~~~~~~~~~~~~~~~~~
Monitors the MTI Moodle platform for upcoming events, midterm grades, and final grades,
then delivers formatted notifications to a Telegram chat.

Supports:
- --test-channel / TEST_CHANNEL=true       : Sends to TEST_CHAT_ID without updating state files.
- --preview-samples / PREVIEW_SAMPLES=true : Sends sample previews to TEST_CHAT_ID without
  requiring new Moodle items or updating state files.
- --dry-run / DRY_RUN=true                 : Logs messages without sending to Telegram or saving state.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
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


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


TELEGRAM_TOKEN: str       = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID: str              = os.getenv("CHAT_ID", "")
TEST_CHAT_ID: str         = os.getenv("TEST_CHAT_ID", "")
UNI_USER: str             = os.getenv("UNI_USER", "")
UNI_PASS: str             = os.getenv("UNI_PASS", "")
ENV_DRY_RUN: bool         = _env_flag("DRY_RUN")
ENV_TEST_CHANNEL: bool    = _env_flag("TEST_CHANNEL")
ENV_PREVIEW_SAMPLES: bool = _env_flag("PREVIEW_SAMPLES")

MOODLE_BASE_URL      = "https://elearning-01.mti.edu.eg"
EVENTS_FILE          = BASE_DIR / "sent_events.json"
GRADES_FILE          = BASE_DIR / "sent_grades.json"
REMINDERS_FILE       = BASE_DIR / "sent_reminders.json"
CAIRO_TZ             = ZoneInfo("Africa/Cairo")
MAX_PERSISTED_IDS    = 500
REQUEST_TIMEOUT      = 25  # seconds
REMINDER_HOURS       = 6   # first reminder before the deadline
FINAL_REMINDER_HOURS = 1   # final reminder before the deadline

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
# Event scraping (Calendar API verified against Dashboard Upcoming Events)
# ---------------------------------------------------------------------------


def extract_dashboard_event_filter(
    dashboard_html: str,
) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(dashboard_html, "html.parser")
    block = soup.select_one("section.block_calendar_upcoming")
    if not block:
        log.warning("Dashboard 'Upcoming events' block not found.")
        return set(), set()

    visible_ids: set[str] = set()
    visible_titles: set[str] = set()

    for item in block.select(".event"):
        ev_id = item.get("data-event-id")
        if ev_id:
            visible_ids.add(str(ev_id).strip())

        link_el = item.select_one("a[href]")
        if link_el:
            title = link_el.get_text(" ", strip=True).lower()
            if title:
                visible_titles.add(title)

            href = link_el.get("href", "")
            m_hash = re.search(r"#event_(\d+)", href)
            if m_hash:
                visible_ids.add(m_hash.group(1))
            m_param = re.search(r"[?&]event=(\d+)", href)
            if m_param:
                visible_ids.add(m_param.group(1))

    log.info(
        "Dashboard Upcoming Events block contains %d event(s).",
        max(len(visible_ids), len(visible_titles)),
    )
    return visible_ids, visible_titles


def fetch_all_moodle_events(
    session: requests.Session,
    sesskey: str,
    dashboard_html: str,
) -> list[dict[str, Any]]:
    visible_ids, visible_titles = extract_dashboard_event_filter(dashboard_html)
    if not visible_ids and not visible_titles:
        return []

    url = (
        f"{MOODLE_BASE_URL}/lib/ajax/service.php"
        f"?sesskey={sesskey}&info=core_calendar_get_calendar_upcoming_view"
    )
    payload: list[dict[str, Any]] = [{
        "index": 0,
        "methodname": "core_calendar_get_calendar_upcoming_view",
        "args": {},
    }]

    resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or not data or data[0].get("error"):
        raise RuntimeError(f"Moodle AJAX error: {data}")

    now_ts = int(datetime.now(tz=CAIRO_TZ).timestamp())
    results: list[dict[str, Any]] = []

    for ev in data[0].get("data", {}).get("events", []):
        ts = ev.get("timesort")
        if ts and ts < now_ts:
            continue

        title    = (ev.get("name") or "Untitled").strip()
        event_id = str(ev.get("id") or "")

        in_dashboard = (
            (event_id and event_id in visible_ids)
            or (title.lower() in visible_titles)
        )
        if not in_dashboard:
            log.info("Skipping event not shown on dashboard: %s", title)
            continue

        link   = ev.get("url") or f"{MOODLE_BASE_URL}/my/"
        course = (ev.get("course", {}).get("fullname") or "Unknown Course").strip()

        raw_desc    = ev.get("description") or ""
        description = (
            BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)
            if raw_desc else ""
        )

        deadline = (
            datetime.fromtimestamp(ts, tz=CAIRO_TZ).strftime("%A, %d %b %Y  %I:%M %p")
            if ts else "Unknown"
        )

        clean_title = title.replace(" is due", "").strip()
        storage_key = clean_title or event_id or f"{title}_{deadline}"

        results.append({
            "id":          storage_key,
            "raw_id":      event_id,
            "title":       title,
            "deadline":    deadline,
            "timesort":    ts,
            "link":        link,
            "course":      course,
            "description": description,
        })

    log.info("Verified %d calendar event(s) against dashboard.", len(results))
    return results


def filter_new_events(
    all_events: list[dict[str, Any]],
    sent_ids: list[str],
) -> list[dict[str, Any]]:
    seen = set(sent_ids)
    new = [
        ev for ev in all_events
        if ev["id"] not in seen and (not ev.get("raw_id") or ev["raw_id"] not in seen)
    ]
    log.info("Found %d new event(s).", len(new))
    return new


def filter_reminder_events(
    all_events: list[dict[str, Any]],
    sent_reminder_ids: list[str],
    skip_event_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now_ts           = datetime.now(tz=CAIRO_TZ).timestamp()
    first_cutoff_ts  = now_ts + REMINDER_HOURS * 3600
    final_cutoff_ts  = now_ts + FINAL_REMINDER_HOURS * 3600
    already_reminded = set(sent_reminder_ids)

    first_reminders: list[dict[str, Any]] = []
    final_reminders: list[dict[str, Any]] = []

    for ev in all_events:
        ts = ev.get("timesort")
        if not ts or not (now_ts < ts <= first_cutoff_ts):
            continue
        if ev["id"] in skip_event_ids:
            continue

        raw_id = ev.get("raw_id") or ""
        final_key = f"{ev['id']}_final"
        first_key = f"{ev['id']}_{REMINDER_HOURS}h"
        old_final_key = f"{raw_id}_final" if raw_id else ""
        old_first_key = f"{raw_id}_{REMINDER_HOURS}h" if raw_id else ""

        if ts <= final_cutoff_ts:
            if final_key not in already_reminded and old_final_key not in already_reminded:
                final_reminders.append({**ev, "reminder_key": final_key})
        else:
            if (
                first_key not in already_reminded
                and ev["id"] not in already_reminded
                and old_first_key not in already_reminded
                and raw_id not in already_reminded
            ):
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

_MIDTERM_KEYWORDS = [
    "midterm",
    "mid term",
    "mid-term",
    "midterms",
    "mid term exam",
    "midterm exam",
    "mt",
    "m.t",
    "m.t.",
    "mid",
    "ميد",
    "ميدتيرم",
    "ميد تيرم",
    "منتصف الفصل",
]

_FINAL_KEYWORDS = [
    "final exam",
    "finals",
    "final",
    "end of term",
    "end-of-term",
    "فاينل",
    "النهائي",
    "الامتحان النهائي",
]


def _detect_exam_type(row_text: str) -> str | None:
    cleaned = re.sub(r"""["'/\\-]+""", " ", row_text.lower())
    text = re.sub(r"\s+", " ", cleaned).strip()
    tokens = set(text.split())

    if any(kw in text for kw in _FINAL_KEYWORDS):
        return "final"

    if any(" " in kw and kw in text for kw in _MIDTERM_KEYWORDS):
        return "midterm"
    if any(" " not in kw and kw in tokens for kw in _MIDTERM_KEYWORDS):
        return "midterm"

    return None


def fetch_grades(
    session: requests.Session,
    dashboard_html: str,
    sent_ids: list[str],
) -> list[dict[str, str]]:
    """Return courses with newly published midterm or final grades immediately."""
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
        mid_gid   = f"{cid}_midterm"
        final_gid = f"{cid}_final"
        if mid_gid in sent_set and final_gid in sent_set:
            continue

        grade_url = f"{MOODLE_BASE_URL}/grade/report/user/index.php?id={cid}"
        try:
            r = session.get(grade_url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            gsoup = BeautifulSoup(r.text, "html.parser")

            for row in gsoup.find_all("tr"):
                row_text = row.get_text(" ", strip=True)
                exam_type = _detect_exam_type(row_text)
                if not exam_type:
                    continue

                gid = f"{cid}_{exam_type}"
                cell = row.select_one("[class*='column-grade']")
                if not cell:
                    tds = row.find_all("td")
                    cell = tds[-1] if len(tds) >= 2 else None
                if not cell:
                    continue

                val = cell.get_text(" ", strip=True)
                log.info(
                    "Matched [%s] (%s) -> val='%s' | already_sent=%s",
                    cname,
                    exam_type,
                    val,
                    gid in sent_set,
                )

                if gid in sent_set:
                    continue

                if val and val not in ("-", "—", "–", ""):
                    results.append({
                        "id": gid,
                        "course": cname,
                        "link": grade_url,
                        "exam_type": exam_type,
                    })
                    sent_set.add(gid)
        except Exception:
            log.warning("Could not retrieve grades for '%s'.", cname, exc_info=True)

    log.info("Found %d new grade(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_message(text: str, target_chat_id: str, dry_run: bool = False) -> bool:
    if dry_run:
        log.info("[DRY RUN -> %s] Preview:\n%s", target_chat_id, text)
        return True

    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            endpoint,
            json={
                "chat_id": target_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Message delivered to Telegram (%s).", target_chat_id)
            return True
        log.error("Telegram rejected the message: %s", r.text)
    except Exception:
        log.error("Telegram request failed.", exc_info=True)
    return False


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

_DIVIDER = "────────────────────"


def _classify_event(event: dict[str, Any]) -> str:
    title = (event.get("title") or "").lower()
    link  = (event.get("link") or "").lower()
    is_quiz = "quiz" in title or "/mod/quiz/" in link

    if not is_quiz:
        return "task"

    if re.search(r"\bclos(e|es|ed|ing)\b", title):
        return "quiz_closes"

    return "quiz_opens"


def build_event_message(event: dict[str, Any]) -> str:
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape

    parts = [
        "📌 <b>ACADEMIC NOTIFICATION</b>",
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
        "",
        f"في assignment جديد نزل يا شباب على المنصة لكورس <b>{e(event['course'])}</b>، خشوا شيكوا عليه.",
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_reminder_message(event: dict[str, Any]) -> str:
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape
    unit_ar    = "ساعة" if REMINDER_HOURS == 1 else "ساعات"
    unit_en    = "hour" if REMINDER_HOURS == 1 else "hours"
    kind       = _classify_event(event)

    if kind == "quiz_closes":
        note = (
            f"الحقوا بسرعة يا شباب، الكويز قرب يقفل — فاضل {REMINDER_HOURS} {unit_ar} بس.\n"
            f"Hurry up! The quiz is closing soon — only {REMINDER_HOURS} {unit_en} left."
        )
    elif kind == "quiz_opens":
        note = (
            f"الكويز ميعاده قرب يا شباب، فاضل {REMINDER_HOURS} {unit_ar} — الحقوا ذاكروا.\n"
            f"The quiz is coming up soon, {REMINDER_HOURS} {unit_en} left — get ready and study!"
        )
    else:
        note = (
            f"المهمة دي هتنتهي خلال {REMINDER_HOURS} {unit_ar} — اتأكد إنك سلّمت.\n"
            f"This task is due in {REMINDER_HOURS} {unit_en} — make sure you submit on time."
        )

    parts = [
        f"⏳ <b>DEADLINE REMINDER  --  {REMINDER_HOURS} {unit_en.upper()} LEFT</b>",
        _DIVIDER,
        f"<b>Course</b>      {e(event['course'])}",
        f"<b>Instructor</b>  {e(instructor)}",
        f"<b>Task</b>        {e(task)}",
        f"<b>Deadline</b>    {e(event['deadline'])}",
        "",
        e(note),
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_final_reminder_message(event: dict[str, Any]) -> str:
    task       = event["title"].replace(" is due", "").strip()
    instructor = resolve_instructor(event["course"])
    e          = html.escape
    unit_en    = "HOUR" if FINAL_REMINDER_HOURS == 1 else "HOURS"
    kind       = _classify_event(event)

    if kind == "quiz_closes":
        note = (
            "الحقوا بسرعة يا شباب، الكويز هيقفل خلاص — ادخلوا وسلّموا قبل ما الوقت يخلص.\n"
            "Hurry up! The quiz is about to close — submit now before time runs out."
        )
    elif kind == "quiz_opens":
        note = (
            "الكويز ميعاده قرب جداً يا شباب — استعدوا وادخلوا قبل ما الوقت يعدي.\n"
            "Final call: the quiz starts very soon — get ready before time runs out."
        )
    else:
        note = (
            "تنبيه أخير: الموعد قرب يقفل — سلّم دلوقتي قبل ما الوقت يخلص.\n"
            "Final reminder: the deadline is closing soon — submit now before time runs out."
        )

    parts = [
        f"🚨 <b>FINAL REMINDER  --  LESS THAN {FINAL_REMINDER_HOURS} {unit_en} LEFT</b>",
        _DIVIDER,
        f"<b>Course</b>      {e(event['course'])}",
        f"<b>Instructor</b>  {e(instructor)}",
        f"<b>Task</b>        {e(task)}",
        f"<b>Deadline</b>    {e(event['deadline'])}",
        "",
        e(note),
        _DIVIDER,
        f"<a href='{e(event['link'], quote=True)}'><b>Open in Moodle</b></a>",
    ]
    return "\n".join(parts)


def build_grade_message(grade: dict[str, str]) -> str:
    e         = html.escape
    exam_type = grade.get("exam_type", "midterm")
    label_ar  = "الفاينل" if exam_type == "final" else "الميد تيرم"
    label_en  = "Final" if exam_type == "final" else "Midterm"

    parts = [
        "🎓 <b>GRADE PUBLISHED</b>",
        _DIVIDER,
        f"درجة <b>{label_ar}</b> بتاعة <b>{e(grade['course'])}</b> نزلت.",
        f"{label_en} grades for <b>{e(grade['course'])}</b> have been published.",
        _DIVIDER,
        f"<a href='{e(grade['link'], quote=True)}'><b>اعرف نتيجتك / Check your grade</b></a>",
    ]
    return "\n".join(parts)


def send_sample_previews(target_chat_id: str, dry_run: bool) -> None:
    now = datetime.now(tz=CAIRO_TZ)
    fmt = "%A, %d %b %Y  %I:%M %p"

    samples = [
        build_event_message({
            "title": "Lab 5 Submission is due",
            "course": "Introduction to Programming",
            "deadline": (now + timedelta(days=2)).strftime(fmt),
            "link": f"{MOODLE_BASE_URL}/my/",
            "description": "Upload your code and flowchart before the deadline.",
        }),
        build_reminder_message({
            "title": "Quiz 1 closes",
            "course": "F26 English Language I",
            "deadline": (now + timedelta(hours=REMINDER_HOURS)).strftime(fmt),
            "link": f"{MOODLE_BASE_URL}/my/",
        }),
        build_final_reminder_message({
            "title": "Assignment 2 is due",
            "course": "Linear Algebra",
            "deadline": (now + timedelta(minutes=40)).strftime(fmt),
            "link": f"{MOODLE_BASE_URL}/my/",
        }),
        build_grade_message({
            "course": "Introduction to Programming",
            "exam_type": "midterm",
            "link": f"{MOODLE_BASE_URL}/my/",
        }),
        build_grade_message({
            "course": "Introduction to Programming",
            "exam_type": "final",
            "link": f"{MOODLE_BASE_URL}/my/",
        }),
    ]

    for msg in samples:
        send_message(msg, target_chat_id=target_chat_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# CLI & Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MTI E-Learning Telegram Notifier")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=ENV_DRY_RUN,
        help="Simulate notifications without sending to Telegram or saving state files.",
    )
    parser.add_argument(
        "--test-channel",
        action="store_true",
        default=ENV_TEST_CHANNEL,
        help="Send notifications to TEST_CHAT_ID without saving state files.",
    )
    parser.add_argument(
        "--preview-samples",
        action="store_true",
        default=ENV_PREVIEW_SAMPLES,
        help="Send sample preview messages to TEST_CHAT_ID without saving state files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dry_run: bool         = args.dry_run
    preview_samples: bool = args.preview_samples
    test_channel: bool    = args.test_channel or preview_samples

    target_chat_id = TEST_CHAT_ID if test_channel else CHAT_ID

    required = []
    if not preview_samples:
        required.extend([("UNI_USER", UNI_USER), ("UNI_PASS", UNI_PASS)])
    if not dry_run:
        required.append(("TELEGRAM_TOKEN", TELEGRAM_TOKEN))
        if test_channel:
            required.append(("TEST_CHAT_ID", TEST_CHAT_ID))
        else:
            required.append(("CHAT_ID", CHAT_ID))

    missing = [name for name, val in required if not val]
    if missing:
        log.error("Missing required environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    skip_state_save = dry_run or test_channel
    if skip_state_save:
        log.info(
            "Non-production run (dry_run=%s, test_channel=%s, preview_samples=%s): "
            "state files will not be modified.",
            dry_run,
            test_channel,
            preview_samples,
        )

    if preview_samples:
        send_sample_previews(target_chat_id=target_chat_id, dry_run=dry_run)
        log.info("Sample previews completed.")
        return

    sent_events    = load_ids(EVENTS_FILE)
    sent_grades    = load_ids(GRADES_FILE)
    sent_reminders = load_ids(REMINDERS_FILE)

    session = build_session()
    dashboard_html, sesskey = authenticate(session)

    all_events = fetch_all_moodle_events(session, sesskey, dashboard_html)
    new_events = filter_new_events(all_events, sent_events)

    sent_new_ids: set[str] = set()
    for ev in new_events:
        if send_message(build_event_message(ev), target_chat_id=target_chat_id, dry_run=dry_run):
            sent_events.append(ev["id"])
            sent_new_ids.add(ev["id"])

    first_reminders, final_reminders = filter_reminder_events(
        all_events, sent_reminders, sent_new_ids
    )

    for ev in first_reminders:
        if send_message(build_reminder_message(ev), target_chat_id=target_chat_id, dry_run=dry_run):
            sent_reminders.append(ev["reminder_key"])

    for ev in final_reminders:
        if send_message(build_final_reminder_message(ev), target_chat_id=target_chat_id, dry_run=dry_run):
            sent_reminders.append(ev["reminder_key"])

    new_grades = fetch_grades(session, dashboard_html, sent_grades)
    for grade in new_grades:
        if send_message(build_grade_message(grade), target_chat_id=target_chat_id, dry_run=dry_run):
            sent_grades.append(grade["id"])

    if skip_state_save:
        log.info("Run completed without modifying state files.")
    else:
        save_ids(EVENTS_FILE, sent_events)
        save_ids(GRADES_FILE, sent_grades)
        save_ids(REMINDERS_FILE, sent_reminders)
        log.info("Run completed successfully.")


if __name__ == "__main__":
    main()
