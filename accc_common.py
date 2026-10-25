#!/usr/bin/env python3
"""
Shared configuration and helpers for the ACCC Acquisitions Register Monitor
===========================================================================
Used by scraper.py, verify.py, documents.py, reporting.py and push_to_sheets.py
so that paths, the ACT holiday calendar, date handling, Canberra time and the
polite HTTP client are defined exactly once.
"""

import datetime
import json
import os
import re
import time
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://www.accc.gov.au"
# NOTE: the ACCC swapped this segment from "mergers-and-acquisitions-registers"
# to "acquisitions-and-mergers-registers" in Aug 2026. The old path still
# 301-redirects, but listing card hrefs now use the new ordering.
REGISTER_PATH = "/public-registers/acquisitions-and-mergers-registers/acquisitions-register"
REGISTER_URL = BASE_URL + REGISTER_PATH

# Stable segment used to identify individual case pages, independent of the
# parent path ordering (guards against future ACCC path reshuffles).
CASE_PATH_MARKER = "/acquisitions-register/"

LISTING_PAGE_SIZE = 50

# ---------------------------------------------------------------------------
# Paths (ACCC_DATA_DIR / ACCC_CACHE_DIR / ACCC_OUTBOX_DIR override for tests)
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("ACCC_DATA_DIR", "data")
# Raw HTML fetched during this run. Not committed. verify.py re-parses these
# files instead of re-downloading pages, which keeps request volume low.
CACHE_DIR = os.environ.get("ACCC_CACHE_DIR", ".cache")
# Email bodies and attachments produced by this run. Not committed.
OUTBOX_DIR = os.environ.get("ACCC_OUTBOX_DIR", "outbox")


def data_path(*parts):
    return os.path.join(DATA_DIR, *parts)


REGISTER_JSON = data_path("register.json")
CHANGELOG_JSON = data_path("changelog.json")
REGISTER_CSV = data_path("register.csv")
REGISTER_XLSX = data_path("register.xlsx")
SUMMARY_JSON = data_path("summary.json")
LATEST_CHANGES_JSON = data_path("latest_changes.json")
STATUS_CSV = data_path("status.csv")
HEALTH_JSON = data_path("health.json")
NEW_DOCUMENTS_JSON = data_path("new_documents.json")
DOCUMENTS_MANIFEST_JSON = data_path("documents_manifest.json")
CONSULTATION_DOCS_DIR = data_path("consultation_docs")
WATCHLIST_JSON = os.environ.get("ACCC_WATCHLIST", "watchlist.json")

LISTING_CACHE_DIR = os.path.join(CACHE_DIR, "html", "listing")
DETAIL_CACHE_DIR = os.path.join(CACHE_DIR, "html", "detail")

# Bump when the stored record format changes. The first run after a bump
# treats formatting-only differences as cosmetic (no alert email flood).
SCHEMA_VERSION = 2

# Human-readable run schedule, shown in emails, Sheets and Excel.
# Keep in sync with the cron expression in .github/workflows/monitor.yml.
SCHEDULE_TEXT = (
    "Mon to Fri, 10:23am and 4:23pm AEST (11:23am and 5:23pm AEDT). "
    "GitHub can start scheduled runs late."
)

# ---------------------------------------------------------------------------
# Canberra time
# ---------------------------------------------------------------------------
# Canberra observes AEST (UTC+10) and AEDT (UTC+11, first Sunday in October to
# first Sunday in April). Fixed "+10 hours" offsets are wrong half the year.
CANBERRA = ZoneInfo("Australia/Sydney")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_now_iso():
    """ISO timestamp in UTC with a trailing Z, to whole seconds."""
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(ts):
    """Parse an ISO 8601 timestamp (with Z or offset). Returns aware datetime or None."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def to_canberra(dt):
    if isinstance(dt, str):
        dt = parse_iso(dt)
    if dt is None:
        return None
    return dt.astimezone(CANBERRA)


def fmt_canberra(dt, fmt="%d %b %Y %H:%M"):
    """Format a UTC datetime/ISO string as Canberra time with AEST/AEDT label."""
    local = to_canberra(dt)
    if local is None:
        return ""
    return f"{local.strftime(fmt)} {local.tzname()}"


def canberra_today():
    return utc_now().astimezone(CANBERRA).date()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
# Date formats the ACCC uses on their pages
DATE_FORMATS = [
    "%d %B %Y",   # "14 May 2026"
    "%d %b %Y",   # "14 May 2026" (abbreviated month)
    "%B %d, %Y",  # "May 14, 2026"
    "%Y-%m-%d",   # "2026-05-14" (ISO, used in meta tags)
    "%d/%m/%Y",   # "14/05/2026"
]


def parse_date(s):
    """Parse a date string into a datetime.date, trying multiple formats."""
    if not s:
        return None
    if isinstance(s, datetime.date):
        return s
    s = clean(str(s))
    # "Sept" is not understood by strptime
    s = re.sub(r"\bSept\b", "Sep", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def iso_date(s):
    """Return YYYY-MM-DD for any parseable date string, else the input unchanged."""
    d = parse_date(s)
    return d.isoformat() if d else (s or "")


def display_date(s):
    """Return '14 May 2026' style for any parseable date, else the input unchanged."""
    d = parse_date(s)
    return f"{d.day} {d.strftime('%b %Y')}" if d else (s or "")


def clean(text):
    """Normalise whitespace in a string for reliable comparison."""
    if not text:
        return ""
    # Replace non-breaking spaces before whitespace normalisation
    text = str(text).replace("\xa0", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text.strip())


# ---------------------------------------------------------------------------
# ACT Public Holiday Calendar — Official ACT Government Source
# ---------------------------------------------------------------------------
# Source: ACT Government Work Safety Group, Chief Minister, Treasury and
# Economic Development Directorate (wsg@act.gov.au)
#   ACT-Public-Holidays-2026.pdf — correct as at 24 March 2026
#   ACT-Public-Holidays-2027.pdf — correct as at 9 December 2025
#   ACT-Public-Holidays-2028.pdf — correct as at 9 December 2025
#   ACT-Public-Holidays-2029.pdf — correct as at 9 December 2025
#   2025 dates derived from the same authority (prior year publication)
#
# When new annual PDFs are published by the ACT Government, add the new
# year's dates here and raise HOLIDAY_TABLE_LAST_YEAR. verify.py warns when
# any register date falls after the last year covered.
# New PDFs are published at: https://www.act.gov.au/working-in-the-act/public-holidays
#
# NOTE: Business day calculations also exclude the ACCC's Christmas/New Year
# suspension period (23 December to 11 January inclusive) per the ACCC
# Merger Process Guidelines.
# ---------------------------------------------------------------------------

_D = datetime.date  # shorthand

ACT_PUBLIC_HOLIDAYS = {

    # 2025 — derived from ACT Government Holidays Act 1958 (ACT) rules
    _D(2025,  1,  1),  # New Year's Day
    _D(2025,  1, 27),  # Australia Day (26 Jan is Sunday, observed Monday)
    _D(2025,  3, 10),  # Canberra Day (second Monday in March)
    _D(2025,  4, 18),  # Good Friday
    _D(2025,  4, 19),  # Easter Saturday
    _D(2025,  4, 20),  # Easter Sunday
    _D(2025,  4, 21),  # Easter Monday
    _D(2025,  4, 25),  # ANZAC Day (Friday)
    _D(2025,  6,  2),  # Reconciliation Day (27 May is Tuesday, following Monday)
    _D(2025,  6,  9),  # King's Birthday (second Monday in June)
    _D(2025, 10,  6),  # Labour Day (first Monday in October)
    _D(2025, 12, 25),  # Christmas Day
    _D(2025, 12, 26),  # Boxing Day

    # 2026 — ACT-Public-Holidays-2026.pdf (correct as at 24 March 2026)
    _D(2026,  1,  1),  # New Year's Day
    _D(2026,  1, 26),  # Australia Day
    _D(2026,  3,  9),  # Canberra Day
    _D(2026,  4,  3),  # Good Friday
    _D(2026,  4,  4),  # Easter Saturday
    _D(2026,  4,  5),  # Easter Sunday
    _D(2026,  4,  6),  # Easter Monday
    _D(2026,  4, 25),  # ANZAC Day (Saturday — observed)
    _D(2026,  4, 27),  # ANZAC Day additional (Monday — as 25 Apr is Saturday)
    _D(2026,  6,  1),  # Reconciliation Day (27 May is Wednesday, following Monday)
    _D(2026,  6,  8),  # King's Birthday
    _D(2026, 10,  5),  # Labour Day
    _D(2026, 12, 25),  # Christmas Day
    _D(2026, 12, 26),  # Boxing Day (Saturday — observed)
    _D(2026, 12, 28),  # Boxing Day additional (Monday — as 26 Dec is Saturday)

    # 2027 — ACT-Public-Holidays-2027.pdf (correct as at 9 December 2025)
    _D(2027,  1,  1),  # New Year's Day
    _D(2027,  1, 26),  # Australia Day
    _D(2027,  3,  8),  # Canberra Day
    _D(2027,  3, 26),  # Good Friday
    _D(2027,  3, 27),  # Easter Saturday
    _D(2027,  3, 28),  # Easter Sunday
    _D(2027,  3, 29),  # Easter Monday
    _D(2027,  4, 26),  # ANZAC Day (25 Apr is Sunday, observed following Monday)
    _D(2027,  5, 31),  # Reconciliation Day (27 May is Thursday, following Monday)
    _D(2027,  6, 14),  # King's Birthday
    _D(2027, 10,  4),  # Labour Day
    _D(2027, 12, 25),  # Christmas Day (Saturday — observed)
    _D(2027, 12, 27),  # Christmas Day additional (Monday — as 25 Dec is Saturday)
    _D(2027, 12, 26),  # Boxing Day (Sunday — observed)
    _D(2027, 12, 28),  # Boxing Day additional (Tuesday — as 26 Dec is Sunday)

    # 2028 — ACT-Public-Holidays-2028.pdf (correct as at 9 December 2025)
    _D(2028,  1,  1),  # New Year's Day (Saturday — observed)
    _D(2028,  1,  3),  # New Year's Day additional (Monday — as 1 Jan is Saturday)
    _D(2028,  1, 26),  # Australia Day
    _D(2028,  3, 13),  # Canberra Day
    _D(2028,  4, 14),  # Good Friday
    _D(2028,  4, 15),  # Easter Saturday
    _D(2028,  4, 16),  # Easter Sunday
    _D(2028,  4, 17),  # Easter Monday
    _D(2028,  4, 25),  # ANZAC Day (Tuesday)
    _D(2028,  5, 29),  # Reconciliation Day (27 May is Saturday, following Monday)
    _D(2028,  6, 12),  # King's Birthday
    _D(2028, 10,  2),  # Labour Day
    _D(2028, 12, 25),  # Christmas Day (Monday)
    _D(2028, 12, 26),  # Boxing Day (Tuesday)

    # 2029 — ACT-Public-Holidays-2029.pdf (correct as at 9 December 2025)
    _D(2029,  1,  1),  # New Year's Day
    _D(2029,  1, 26),  # Australia Day
    _D(2029,  3, 12),  # Canberra Day
    _D(2029,  3, 30),  # Good Friday
    _D(2029,  3, 31),  # Easter Saturday
    _D(2029,  4,  1),  # Easter Sunday
    _D(2029,  4,  2),  # Easter Monday
    _D(2029,  4, 25),  # ANZAC Day (Wednesday)
    _D(2029,  5, 28),  # Reconciliation Day (27 May is Sunday, following Monday)
    _D(2029,  6, 11),  # King's Birthday
    _D(2029, 10,  1),  # Labour Day
    _D(2029, 12, 25),  # Christmas Day (Tuesday)
    _D(2029, 12, 26),  # Boxing Day (Wednesday)
}

HOLIDAY_TABLE_LAST_YEAR = 2029


def in_christmas_suspension(d):
    """ACCC Christmas/New Year suspension (23 Dec to 11 Jan inclusive)."""
    return (d.month == 12 and d.day >= 23) or (d.month == 1 and d.day <= 11)


def is_accc_business_day(d):
    """
    Returns True if d counts as a business day under the ACCC merger regime.
    Excludes weekends, ACT public holidays (official ACT Government PDFs),
    and the 23 Dec to 11 Jan Christmas/New Year suspension period per the
    ACCC Merger Process Guidelines.
    """
    if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    if d in ACT_PUBLIC_HOLIDAYS:
        return False
    if in_christmas_suspension(d):
        return False
    return True


def count_accc_business_days(start_date, end_date):
    """
    Count ACCC business days AFTER start_date, up to and including end_date.

    The Effective Notification Date is "Day 0" of the review period, so the
    first business day counted is the one after it. A Phase 1 matter notified
    on 11 Sep 2026 with an end of determination period of 26 Oct 2026 is
    therefore 30 business days (the statutory Phase 1 length), not 31.
    """
    if not start_date or not end_date or end_date < start_date:
        return None
    count = 0
    current = start_date + datetime.timedelta(days=1)
    while current <= end_date:
        if is_accc_business_day(current):
            count += 1
        current += datetime.timedelta(days=1)
    return count


def calc_period_days(start_str, end_str):
    """
    Return (calendar_days, accc_business_days) from start date (Day 0) to end
    date. Both measures exclude the start day and include the end day.
    """
    start = parse_date(start_str)
    end = parse_date(end_str)
    if not start or not end or end < start:
        return None, None
    return (end - start).days, count_accc_business_days(start, end)


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj, indent=2):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent, default=str, ensure_ascii=False)
    os.replace(tmp, path)  # atomic: never leaves a half-written file


def write_github_outputs(**values):
    """Append key=value pairs to $GITHUB_OUTPUT (no-op outside Actions)."""
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if not gh_output:
        return
    with open(gh_output, "a", encoding="utf-8") as f:
        for key, val in values.items():
            if isinstance(val, bool):
                val = "true" if val else "false"
            val = str(val).replace("\n", " ")
            f.write(f"{key}={val}\n")


# ---------------------------------------------------------------------------
# HTTP: a polite, rate-limited client that stops early when blocked
# ---------------------------------------------------------------------------
# Polite delay between HTTP requests (seconds)
REQUEST_DELAY = float(os.environ.get("ACCC_REQUEST_DELAY", "1.5"))
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
MAX_RETRY_AFTER = 120          # never sleep longer than this on Retry-After
BLOCKED_STATUS_LIMIT = 3       # consecutive 403s before the run gives up

USER_AGENT_BASE = (
    "ACCC-Register-Monitor/2.0 "
    "(automated monitor of the ACCC public acquisitions register; "
    "low request rate"
)


def build_user_agent():
    """
    Identify the tool honestly. A contact (email or URL) is optional and can
    be added later via the MONITOR_CONTACT repository variable without a code
    change.
    """
    contact = clean(os.environ.get("MONITOR_CONTACT", ""))
    if contact:
        return f"{USER_AGENT_BASE}; contact: {contact})"
    return USER_AGENT_BASE + ")"


class AccessBlocked(Exception):
    """The ACCC site is refusing our requests (HTTP 403). Retrying will not help."""


class FetchResult:
    def __init__(self, url, status, text="", headers=None, error=""):
        self.url = url
        self.status = status          # int HTTP status, 304, or 0 for network error
        self.text = text
        self.headers = headers or {}
        self.error = error

    @property
    def ok(self):
        return self.status == 200

    @property
    def not_modified(self):
        return self.status == 304


class PoliteSession:
    """
    Wraps requests.Session with:
      - one request at a time, REQUEST_DELAY seconds apart
      - conditional requests (If-None-Match / If-Modified-Since) so unchanged
        pages can be answered with a cheap 304
      - no pointless retries on 4xx (403/404/410)
      - Retry-After support on 429/503
      - a circuit breaker: after BLOCKED_STATUS_LIMIT consecutive 403s the run
        stops instead of continuing to hit the site
    """

    def __init__(self, session=None, delay=REQUEST_DELAY):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": build_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        })
        self.delay = delay
        self.request_count = 0
        self.consecutive_403 = 0
        self._last_request_at = 0.0

    def _wait_turn(self):
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get(self, url, etag="", last_modified="", timeout=REQUEST_TIMEOUT,
            retries=MAX_RETRIES, binary=False):
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error = ""
        for attempt in range(retries):
            self._wait_turn()
            self._last_request_at = time.monotonic()
            self.request_count += 1
            try:
                resp = self.session.get(url, timeout=timeout, headers=headers)
            except requests.RequestException as exc:
                last_error = str(exc)
                print(f"      Attempt {attempt + 1}/{retries} failed for {url}: {exc}")
                if attempt < retries - 1:
                    time.sleep(self.delay * (2 ** attempt))
                continue

            status = resp.status_code
            if status == 403:
                self.consecutive_403 += 1
                print(f"      403 Forbidden: {url}")
                if self.consecutive_403 >= BLOCKED_STATUS_LIMIT:
                    raise AccessBlocked(
                        f"The ACCC website returned 403 Forbidden for "
                        f"{self.consecutive_403} requests in a row (last: {url}). "
                        "The site's CDN is refusing requests from this machine."
                    )
                return FetchResult(url, status, error="403 Forbidden")
            self.consecutive_403 = 0

            if status in (429, 503) and attempt < retries - 1:
                wait = _retry_after_seconds(resp.headers.get("Retry-After"), self.delay * (2 ** attempt))
                print(f"      {status} from server, waiting {wait:.0f}s before retrying {url}")
                time.sleep(wait)
                continue
            if status >= 500 and attempt < retries - 1:
                print(f"      Attempt {attempt + 1}/{retries}: HTTP {status} for {url}")
                time.sleep(self.delay * (2 ** attempt))
                continue

            text = resp.content if binary else resp.text
            return FetchResult(url, status, text, dict(resp.headers),
                               "" if status in (200, 304) else f"HTTP {status}")

        print(f"      SKIPPING (all retries exhausted): {url}")
        return FetchResult(url, 0, error=last_error or "retries exhausted")


def _retry_after_seconds(value, default):
    if not value:
        return min(default, MAX_RETRY_AFTER)
    try:
        return min(max(float(value), 1.0), MAX_RETRY_AFTER)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
        return min(max((when - utc_now()).total_seconds(), 1.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return min(default, MAX_RETRY_AFTER)


# ---------------------------------------------------------------------------
# HTML cache (raw pages from this run, re-used by verify.py)
# ---------------------------------------------------------------------------

def _safe_name(slug):
    return re.sub(r"[^A-Za-z0-9._-]", "_", slug)[:180]


def cache_listing_html(page, html):
    os.makedirs(LISTING_CACHE_DIR, exist_ok=True)
    with open(os.path.join(LISTING_CACHE_DIR, f"page-{page:03d}.html"), "w", encoding="utf-8") as f:
        f.write(html)


def cache_detail_html(slug, html):
    os.makedirs(DETAIL_CACHE_DIR, exist_ok=True)
    with open(os.path.join(DETAIL_CACHE_DIR, _safe_name(slug) + ".html"), "w", encoding="utf-8") as f:
        f.write(html)


def cached_detail_html(slug):
    path = os.path.join(DETAIL_CACHE_DIR, _safe_name(slug) + ".html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def cached_listing_pages():
    if not os.path.isdir(LISTING_CACHE_DIR):
        return []
    pages = []
    for name in sorted(os.listdir(LISTING_CACHE_DIR)):
        with open(os.path.join(LISTING_CACHE_DIR, name), "r", encoding="utf-8") as f:
            pages.append(f.read())
    return pages


# ---------------------------------------------------------------------------
# Run health
# ---------------------------------------------------------------------------

def load_health():
    return load_json(HEALTH_JSON, {
        "last_attempt_utc": "",
        "last_success_utc": "",
        "last_status": "",
        "last_error": "",
        "consecutive_failures": 0,
        "last_request_count": 0,
        "last_digest_week": "",
    })


def data_age_days(health=None):
    """Whole days since the last successful scrape, or None if never."""
    health = health if health is not None else load_health()
    last = parse_iso(health.get("last_success_utc", ""))
    if not last:
        return None
    return (utc_now() - last).days
