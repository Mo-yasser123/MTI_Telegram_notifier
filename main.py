"""
MTI E-Learning Notifier
~~~~~~~~~~~~~~~~~~~~~~~
Scrapes the MTI Moodle platform for upcoming academic events,
6-hour deadline reminders, and newly published midterm grades,
then delivers real-time notifications to a designated Telegram chat.

Designed to run as a scheduled GitHub Actions workflow or via any
external cron trigger (e.g. cron-job.org).
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
CHAT_ID: str = os.getenv("CHAT_ID", "")
UNI_USER: str = os.getenv("UNI_USER", "")
UNI_PASS: str = os.getenv("UNI_PASS", "")

MOODLE_BASE_URL = "https://elearning-01.mti.edu.eg"
EVENTS_FILE = BASE_DIR / "sent_events.json"
GRADES_FILE = BASE_DIR / "sent_grades.json"
CAIRO_TZ = ZoneInfo("Africa/Cairo")
MAX_PERSISTED_IDS = 500
REQUEST_TIMEOUT = 25  # seconds
REMINDER_HOURS = 6

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mti_notifier")

# ---------------------------------------------------------------------------
# Professor mapping
# ---------------------------------------------------------------------------

_PROFESSOR_MAP: list[tuple[str, str]] = [
    ("english language", "Dr. Rasha Saeed"),
    ("introduction to programming", "Dr. Hafez M.S. Abdel-Wahab"),
    ("introduction to computer science", "Dr. Mohamed M. Elgazzar"),
    ("linear algebra", "Walid Basiony"),
    ("mathematical analysis", "Dr. Rania Ahmed"),
    ("entrepreneur", "Dr. El-Sayed Orabi"),
]


def resolve_professor(course_name: str) -> str:
    """Return the professor associated with *course_name*, if known."""
    lowered = course_name.lower()
    for keyword, professor in _PROFESSOR_MAP:
        if keyword in lowered:
            return professor
    return "N/A"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def load_id_list(path: Path) -> list[str]:
    """Load a JSON list of string IDs from *path*."""
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_id_list(path: Path, ids: list[str]) -> None:
    """Persist the most recent *MAX_PERSISTED_IDS* entries to *path*."""
    with path.open("w", encoding="utf-8") as fh:
        json.dump(ids[-MAX_PERSISTED_IDS:], fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTTP session factory
# ---------------------------------------------------------------------------


def create_session() -> requests.Session:
    """Build a ``requests.Session`` with sensible retry behaviour."""
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
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Moodle authentication
# ---------------------------------------------------------------------------


def authenticate(session: requests.Session) -> tuple[str, str]:
    """
    Log in to Moodle and return *(dashboard_html, sesskey)*.

    Raises ``RuntimeError`` on failure.
    """
    login_url = f"{MOODLE_BASE_URL}/login/index.php"

    # Fetch the login page to obtain the CSRF token.
    page = session.get(login_url, timeout=REQUEST_TIMEOUT)
    page.raise_for_status()

    token_match = re.search(r'name="logintoken"\s+value="([^"]+)"', page.text)
    if not token_match:
        raise RuntimeError("Login token not found on the Moodle login page.")

    # Submit credentials.
    response = session.post(
        login_url,
        data={
            "username": UNI_USER,
            "password": UNI_PASS,
            "logintoken": token_match.group(1),
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    if "login/index.php" in response.url:
        raise RuntimeError(
            "Moodle authentication failed — verify UNI_USER and UNI_PASS."
        )

    # Load the dashboard to extract the session key.
    dashboard = session.get(f"{MOODLE_BASE_URL}/my/", timeout=REQUEST_TIMEOUT)
    dashboard.raise_for_status()

    sesskey_match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', dashboard.text)
    if not sesskey_match:
        raise RuntimeError("Session key not found on the Moodle dashboard.")

    logger.info("Moodle authentication successful.")
    return dashboard.text, sesskey_match.group(1)


# ---------------------------------------------------------------------------
# Event scraping & 6-hour deadline reminders
# ---------------------------------------------------------------------------


def fetch_events(
    session: requests.Session,
    sesskey: str,
    already_sent: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return *(new_events, reminder_events)* from the Moodle calendar."""

    ajax_url = (
        f"{MOODLE_BASE_URL}/lib/ajax/service.php"
        f"?sesskey={sesskey}"
        f"&info=core_calendar_get_action_events_by_timesort"
    )
    payload = [
        {
            "index": 0,
            "methodname": "core_calendar_get_action_events_by_timesort",
            "args": {"limitnum": 26, "timesortfrom": 0},
        }
    ]

    resp = session.post(ajax_url, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()

    if not isinstance(data, list) or not data or data[0].get("error"):
        raise RuntimeError(f"Moodle AJAX returned an error: {data}")

    raw_events: list[dict[str, Any]] = (
        data[0].get("data", {}).get("events", [])
    )

    seen: set[str] = set(already_sent)
    now_ts = datetime.now(tz=CAIRO_TZ).timestamp()
    reminder_window = REMINDER_HOURS * 3600

    new_events: list[dict[str, str]] = []
    reminder_events: list[dict[str, str]] = []

    for ev in raw_events:
        title = (ev.get("name") or "Untitled").strip()
        link = ev.get("url") or f"{MOODLE_BASE_URL}/my/"
        course = (
            ev.get("course", {}).get("fullname") or "Unknown Course"
        ).strip()

        raw_desc = ev.get("description") or ""
        description = (
            BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)
            if raw_desc
            else ""
        )

        timesort = ev.get
