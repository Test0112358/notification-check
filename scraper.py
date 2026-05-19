#!/usr/bin/env python3
"""
ACCC Acquisitions Register Monitor
====================================
Scrapes every entry on the ACCC acquisitions register, detects any field-level
changes (including date changes on individual case pages), and exports the full
dataset as JSON, CSV, and a structured changelog.

Designed to run on a schedule via GitHub Actions and commit results back to the
repository so the data directory acts as a persistent, version-controlled database.

Confirmed against live pages on 2026-05-18:
  - Detail page structure: h3 headings followed by values (Drupal 10 CMS)
  - article:modified_time meta tag present on all pages — used as primary change signal
  - "End of determination period" only on notification-type detail pages
  - "ACCC Determination" + "Determination publication date" only on completed cases
  - "Status" h3 heading exists but is consistently blank; "Acquisition status" is the real one
  - Waiver cases use "Waiver application date" instead of "Effective notification date"
  - No field named "ACCC Determination Period" exists on the site;
    business/calendar day columns are calculated from notification → end of det. period
    (or → determination publication date for completed cases)
"""

import json
import os
import re
import sys
import time
import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://www.accc.gov.au"
REGISTER_PATH = "/public-registers/mergers-and-acquisitions-registers/acquisitions-register"
REGISTER_URL = BASE_URL + REGISTER_PATH

DATA_DIR = "data"
REGISTER_JSON = os.path.join(DATA_DIR, "register.json")
CHANGELOG_JSON = os.path.join(DATA_DIR, "changelog.json")
REGISTER_CSV = os.path.join(DATA_DIR, "register.csv")
SUMMARY_JSON = os.path.join(DATA_DIR, "summary.json")
LATEST_CHANGES_JSON = os.path.join(DATA_DIR, "latest_changes.json")

# Polite delay between HTTP requests (seconds)
REQUEST_DELAY = 1.2
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "ACCC-Register-Monitor/1.0 "
        "(automated research tool; github.com/YOUR-ORG/accc-monitor)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# ACT public holiday calendar (includes Canberra Day, Family & Community Day, etc.)

# Date formats the ACCC uses on their pages
DATE_FORMATS = [
    "%d %B %Y",   # "14 May 2026"
    "%d %b %Y",   # "14 May 2026" (abbreviated month)
    "%B %d, %Y",  # "May 14, 2026"
    "%Y-%m-%d",   # "2026-05-14" (ISO, used in meta tags)
]

# Fields compared for change detection. Changing any of these triggers a changelog entry.
TRACKED_FIELDS = [
    "title",
    "acquisition_status",
    "type",
    "case_number",
    "stage",
    "notification_date",
    "end_of_determination_period",
    "accc_determination",
    "determination_publication_date",
    "acquirers",
    "targets",
    "other_parties",
    "anzsic_codes",
    "last_modified_accc",  # article:modified_time from the ACCC page
    "consultation_text",
    "consultation_docs",
    "decisions_docs",  
]

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
# year's dates here and update requirements.txt if needed.
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
    # ACCC Christmas/New Year suspension (23 Dec to 11 Jan inclusive)
    if (d.month == 12 and d.day >= 23) or (d.month == 1 and d.day <= 11):
        return False
    return True


def count_accc_business_days(start_date, end_date):
    """
    Count ACCC business days from start_date to end_date inclusive.
    """
    if not start_date or not end_date or end_date < start_date:
        return None
    count = 0
    current = start_date
    while current <= end_date:
        if is_accc_business_day(current):
            count += 1
        current += datetime.timedelta(days=1)
    return count
# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def parse_date(s):
    """Parse a date string into a datetime.date, trying multiple formats."""
    if not s:
        return None
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def calc_determination_days(notification_date_str, end_date_str):
    """
    Return (calendar_days, accc_business_days) between two date strings.
    Calendar days: simple inclusive date difference.
    ACCC business days: uses official ACT Government holiday dates and
    excludes the ACCC's 23 Dec to 11 Jan suspension period.
    """
    start = parse_date(notification_date_str)
    end = parse_date(end_date_str)
    if not start or not end or end < start:
        return None, None
    calendar_days = (end - start).days
    business_days = count_accc_business_days(start, end)
    return calendar_days, business_days


def clean(text):
    """Normalise whitespace in a string for reliable comparison."""
    if not text:
        return ""
    # Replace non-breaking spaces before whitespace normalisation
    text = text.replace("\xa0", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text.strip())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url, session, retries=MAX_RETRIES):
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            wait = REQUEST_DELAY * (2 ** attempt)
            print(f"      Attempt {attempt + 1}/{retries} failed for {url}: {exc}")
            if attempt < retries - 1:
                time.sleep(wait)
    print(f"      SKIPPING (all retries exhausted): {url}")
    return None


# ---------------------------------------------------------------------------
# Listing page parsing
# ---------------------------------------------------------------------------

def get_last_page_index(soup):
    """Extract the 0-based index of the last page from pagination links."""
    # Drupal pager: "Go to last page" link
    last_link = soup.find("a", title=re.compile(r"Go to last page", re.I))
    if last_link:
        m = re.search(r"page=(\d+)", last_link.get("href", ""))
        if m:
            return int(m.group(1))
    # Fallback: find the highest page= value among all pager links
    max_page = 0
    for a in soup.select("li.pager__item a, .pager a"):
        m = re.search(r"page=(\d+)", a.get("href", ""))
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def parse_card(a_tag):
    """
    Parse a single acquisition card <a> element from the listing page.
    The card contains the title + structured key/value labels as child elements.
    Returns a dict with summary-level fields, or None if the link is not a case page.
    """
    href = a_tag.get("href", "")

    # Must be an individual case page (not the register index or a filter link)
    if not href.startswith(REGISTER_PATH + "/"):
        return None
    if "?" in href or "#" in href:
        return None

    full_url = urljoin(BASE_URL, href)
    slug = href.rstrip("/").split("/")[-1]

    # get_text(separator="\n") places each element's text on its own line,
    # giving us a list we can iterate through to find label→value pairs.
    lines = [
        l.strip()
        for l in a_tag.get_text(separator="\n").splitlines()
        if l.strip()
    ]

    # The first line is the case title (before any field labels)
    KNOWN_LABELS = {
        "acquisition status",
        "type",
        "case number",
        "stage",
        "effective notification date",
        "waiver application date",
        "notification / application date",
    }
    title = ""
    for i, line in enumerate(lines):
        if line.lower() in KNOWN_LABELS or any(line.lower().startswith(k) for k in KNOWN_LABELS):
            title = " ".join(lines[:i]).strip()
            break
    if not title and lines:
        title = lines[0]

    entry = {
        "slug": slug,
        "url": full_url,
        "title": clean(title),
    }

    # Map label (lowercased) to field key
    LABEL_MAP = {
        "acquisition status": "acquisition_status",
        "type": "type",
        "case number": "case_number",
        "stage": "stage",
        "effective notification date": "notification_date",
        "waiver application date": "notification_date",
        "notification / application date": "notification_date",
    }

    i = 0
    while i < len(lines):
        lower = lines[i].lower()
        for label, field in LABEL_MAP.items():
            if lower == label:
                if i + 1 < len(lines):
                    entry[field] = clean(lines[i + 1])
                i += 1
                break
        i += 1

    return entry


def fetch_all_listing_entries(session):
    """
    Paginate through all listing pages and return a dict of {slug: entry}.
    Uses 50 items per page to minimise total requests.
    """
    print("  Fetching register listing pages...")
    entries = {}

    first_url = f"{REGISTER_URL}?items_per_page=50&page=0"
    soup = fetch(first_url, session)
    if not soup:
        print("ERROR: Could not fetch the register listing page. Aborting.")
        sys.exit(1)

    last_page = get_last_page_index(soup)
    total_pages = last_page + 1
    print(f"  {total_pages} page(s) found at 50 items/page.")

    def parse_soup(soup):
        for a in soup.find_all("a", href=True):
            card = parse_card(a)
            if card and card["slug"] not in entries:
                entries[card["slug"]] = card

    parse_soup(soup)
    time.sleep(REQUEST_DELAY)

    for page in range(1, total_pages):
        url = f"{REGISTER_URL}?items_per_page=50&page={page}"
        print(f"    Page {page + 1}/{total_pages}...")
        page_soup = fetch(url, session)
        if page_soup:
            parse_soup(page_soup)
        time.sleep(REQUEST_DELAY)

    print(f"  Found {len(entries)} unique entries on the listing.")
    return entries


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def get_section_table(h3_tag, all_h3_and_tables):
    """
    Return the first table belonging to this h3 section —
    the first table appearing after this h3 and before the next h3
    in document order.
    """
    found = False
    for el in all_h3_and_tables:
        if el is h3_tag:
            found = True
            continue
        if found:
            if el.name == "h3":
                return None
            if el.name == "table":
                return el
    return None


def parse_table_rows(table_tag):
    """
    Extract rows from a section table as a list of
    {date, description, url} dicts. Skips header rows.
    """
    rows = []
    if not table_tag:
        return rows
    for row in table_tag.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        date_text = clean(cells[0].get_text())
        desc_text = clean(cells[1].get_text())
        if date_text.lower() in ("date", ""):
            continue
        link_tag = None
        for cell in cells:
            link_tag = cell.find("a", href=True)
            if link_tag:
                break
        doc_url = urljoin(BASE_URL, link_tag["href"]) if link_tag else ""
        if date_text and desc_text:
            rows.append({
                "date": date_text,
                "description": desc_text,
                "url": doc_url,
            })
    return rows


def format_docs_for_csv(docs):
    """
    Format a list of document dicts as a pipe-separated readable string.
    Example: "3 Apr 2026: Statement of Issues | 15 May 2026: Determination"
    """
    if not docs:
        return ""
    return " | ".join(
        f"{d.get('date', '')}: {d.get('description', '')}"
        for d in docs
        if d.get("description")
    )

def next_sibling_text(h3_tag):
    """
    Return the cleaned text content of the first meaningful sibling after an h3.
    Skips whitespace-only text nodes. Returns None if the next sibling is another h3
    or a list (those are handled separately).
    """
    sib = h3_tag.next_sibling
    while sib is not None:
        if hasattr(sib, "name"):
            if sib.name in ("p", "div", "span"):
                text = clean(sib.get_text())
                if text:
                    return text
            elif sib.name in ("ul", "ol", "h3", "h2", "h1"):
                return None  # stop looking
        elif isinstance(sib, str):
            text = sib.strip()
            if text:
                return text
        sib = sib.next_sibling
    return None


def extract_parties_from_text(page_text, section_label, stop_labels):
    """
    Extract party names from raw page text between a section header and the next.
    Bypasses DOM structure entirely — works regardless of Drupal's HTML nesting.
    """
    # Find the section label (as its own line)
    lines = page_text.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        if clean(line).lower() == section_label.lower():
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    # Collect lines until we hit another known section header
    stop_set = {s.lower() for s in stop_labels}
    items = []
    for line in lines[start_idx:]:
        stripped = clean(line)
        if stripped.lower() in stop_set:
            break
        # Skip very short lines (bullet markers, empty spans, whitespace)
        if len(stripped) > 5:
            items.append(stripped)

    return items


def parse_detail_page(soup, url):
    """Parse an individual acquisition/waiver detail page."""
    data = {"url": url}

    if not soup:
        return data

    # Meta tags
    meta_mod = soup.find("meta", {"property": "article:modified_time"})
    if meta_mod:
        data["last_modified_accc"] = clean(meta_mod.get("content", ""))

    meta_pub = soup.find("meta", {"property": "article:published_time"})
    if meta_pub:
        data["published_time_accc"] = clean(meta_pub.get("content", ""))

    # Page title
    h1 = soup.find("h1")
    if h1:
        data["title"] = clean(h1.get_text())

    # Scalar fields via h3 heading navigation
    SCALAR_FIELDS = {
        "acquisition status":              "acquisition_status",
        "acquisition case number":         "case_number",
        "type":                            "type",
        "effective notification date":     "notification_date",
        "waiver application date":         "notification_date",
        "notification / application date": "notification_date",
        "stage":                           "stage",
        "end of determination period":     "end_of_determination_period",
        "accc determination":              "accc_determination",
        "determination publication date":  "determination_publication_date",
        "anzsic code(s)":                  "anzsic_codes",
        "anzsic codes":                    "anzsic_codes",
    }

    for h3 in soup.find_all("h3"):
        label = clean(h3.get_text()).lower()
        if label in SCALAR_FIELDS:
            val = next_sibling_text(h3)
            if val:
                data[SCALAR_FIELDS[label]] = val

    # Party fields via text extraction
    page_text = soup.get_text(separator="\n")

    ALL_STOP_LABELS = [
        "Acquirer(s)", "Target(s) or Vendor(s)", "Other party(ies)",
        "ANZSIC code(s)", "ANZSIC codes", "Description", "Consultation",
        "Decisions and key events", "About the acquisition",
        "ACCC Determination", "Determination publication date",
        "Acquisition status", "Acquisition case number",
    ]

    acquirers = extract_parties_from_text(
        page_text, "Acquirer(s)",
        [s for s in ALL_STOP_LABELS if s != "Acquirer(s)"]
    )
    if acquirers:
        data["acquirers"] = "; ".join(acquirers)

    targets = extract_parties_from_text(
        page_text, "Target(s) or Vendor(s)",
        [s for s in ALL_STOP_LABELS if s != "Target(s) or Vendor(s)"]
    )
    if targets:
        data["targets"] = "; ".join(targets)

    others = extract_parties_from_text(
        page_text, "Other party(ies)",
        [s for s in ALL_STOP_LABELS if s != "Other party(ies)"]
    )
    if others:
        data["other_parties"] = "; ".join(others)

   # ── Section-aware document and text extraction ─────────────────────────
    all_h3_and_tables = soup.find_all(["h3", "table"])

    for h3 in soup.find_all("h3"):
        label = clean(h3.get_text()).lower()

        if label == "consultation":
            # Prose text from consultation section
            text_parts = []
            sib = h3.next_sibling
            while sib is not None:
                if hasattr(sib, "name"):
                    if sib.name == "h3":
                        break
                    if sib.name in ("p", "div"):
                        t = clean(sib.get_text())
                        if t and len(t) > 15:
                            text_parts.append(t)
                sib = sib.next_sibling
            if text_parts:
                data["consultation_text"] = " ".join(text_parts[:3])

            # Consultation document table (Word docs or PDFs)
            table = get_section_table(h3, all_h3_and_tables)
            rows = parse_table_rows(table)
            if rows:
                data["consultation_docs"] = rows

        elif label in (
            "decisions and key events",
            "key events",
            "decisions",
            "decisions & key events",
        ):
            # Decisions document table (PDFs or Word docs)
            table = get_section_table(h3, all_h3_and_tables)
            rows = parse_table_rows(table)
            if rows:
                data["decisions_docs"] = rows

    # Combined documents field for backward compatibility
    all_docs = (
        data.get("consultation_docs", []) +
        data.get("decisions_docs", [])
    )
    if all_docs:
        data["documents"] = all_docs

    return data


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(old, new):
    """
    Compare two entry dicts across TRACKED_FIELDS.
    Returns a list of {"field", "old", "new"} dicts for any field that changed.
    """
    changes = []
    for field in TRACKED_FIELDS:
        old_val = clean(str(old.get(field, "") or ""))
        new_val = clean(str(new.get(field, "") or ""))
        if old_val != new_val:
            changes.append({"field": field, "old": old_val, "new": new_val})
    return changes

def format_date_for_csv(date_str):
    """
    Convert any date string to ISO format (YYYY-MM-DD) for Google Sheets.
    ISO format sorts correctly both alphabetically and as date values.
    Returns the original string unchanged if it cannot be parsed.
    """
    d = parse_date(date_str)
    if d:
        return d.strftime("%Y-%m-%d")
    return date_str
# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str, ensure_ascii=False)

def format_date_for_csv(date_str):
    """Convert date strings to ISO YYYY-MM-DD format for correct sorting."""
    d = parse_date(date_str)
    if d:
        return d.strftime("%Y-%m-%d")
    return date_str

def export_csv(register):
    """Write the register data to a CSV with human-readable column names."""
    os.makedirs(DATA_DIR, exist_ok=True)
    rows = []
    for slug, e in register.items():
        rows.append({
            "Acquisition Name":                         e.get("title", ""),
            "Type":                                     e.get("type", ""),
            "Case Number":                              e.get("case_number", ""),
            "Acquirer(s)":                              e.get("acquirers", ""),
            "Target(s) / Vendor(s)":                   e.get("targets", ""),
            "Other Parties":                            e.get("other_parties", ""),
            "Notification / Application Date":  format_date_for_csv(e.get("notification_date", "")),
            "Stage":                                    e.get("stage", ""),
            "Acquisition Status":                       e.get("acquisition_status", ""),
            "End of Determination Period":       format_date_for_csv(e.get("end_of_determination_period", "")),
            # Calculated span: notification date → end of determination period
            "Determination Period Calendar Days (ACT)": e.get("det_period_calendar_days", ""),
            "Determination Period Business Days (ACT)": e.get("det_period_business_days", ""),
            # For completed cases: notification date → determination publication date
            "Elapsed Calendar Days to Decision":        e.get("elapsed_calendar_days", ""),
            "Elapsed Business Days to Decision (ACT)":  e.get("elapsed_business_days", ""),
            "ACCC Determination":                       e.get("accc_determination", ""),
            "Determination Publication Date":    format_date_for_csv(e.get("determination_publication_date", "")),
            "ANZSIC Code(s)":                           e.get("anzsic_codes", ""),
            "URL":                                      e.get("url", ""),
            "Decisions and Key Events":  format_docs_for_csv(e.get("decisions_docs", [])),
            "Consultation Text":          e.get("consultation_text", ""),
            "Consultation Documents":     format_docs_for_csv(e.get("consultation_docs", [])),
            "ACCC Page Last Modified":                  e.get("last_modified_accc", ""),
            "Last Scraped (UTC)":                       e.get("last_scraped_utc", ""),
        })
    df = pd.DataFrame(rows)
    # utf-8-sig BOM so Excel opens it correctly without a manual encoding step
    df.to_csv(REGISTER_CSV, index=False, encoding="utf-8-sig")
    print(f"  CSV exported: {len(rows)} rows → {REGISTER_CSV}")


def generate_summary(register):
    """Produce aggregate statistics for the dashboard / README."""
    total = len(register)
    by_status = {}
    by_stage = {}
    by_type = {}
    by_determination = {}
    pending_decision = []

    for slug, e in register.items():
        s = e.get("acquisition_status", "Unknown")
        by_status[s] = by_status.get(s, 0) + 1

        st = e.get("stage", "")
        if st:
            by_stage[st] = by_stage.get(st, 0) + 1

        t = e.get("type", "Unknown")
        by_type[t] = by_type.get(t, 0) + 1

        d = e.get("accc_determination", "")
        if d:
            by_determination[d] = by_determination.get(d, 0) + 1

        # Flag active cases approaching their end of determination period
        if s == "Under assessment" and e.get("end_of_determination_period"):
            end = parse_date(e["end_of_determination_period"])
            today = datetime.date.today()
            if end and (end - today).days <= 10:
                pending_decision.append({
                    "slug": slug,
                    "title": e.get("title", ""),
                    "end_of_determination_period": e["end_of_determination_period"],
                    "days_remaining": (end - today).days,
                })

    return {
        "generated_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "total_entries": total,
        "by_acquisition_status": by_status,
        "by_stage": by_stage,
        "by_type": by_type,
        "by_accc_determination": by_determination,
        "decisions_due_within_10_days": sorted(
            pending_decision, key=lambda x: x["days_remaining"]
        ),
    }

def detect_new_questionnaires(stored_register, new_register):
    """
    Detect new consultation questionnaire Word documents (.docx) only.
    Checks the Consultation section of each entry.
    Skips PDFs, blank URLs, and any doc already in the stored register.
    """
    new_questionnaires = []

    for slug, entry in new_register.items():
        stored = stored_register.get(slug, {})
        old_urls = {
            d["url"] for d in stored.get("consultation_docs", [])
            if d.get("url")
        }
        for doc in entry.get("consultation_docs", []):
            url = doc.get("url", "")
            if (url
                    and url not in old_urls
                    and url.lower().endswith(".docx")):
                new_questionnaires.append({
                    "slug": slug,
                    "title": entry.get("title", slug),
                    "case_number": entry.get("case_number", ""),
                    "accc_url": entry.get("url", ""),
                    "date": doc.get("date", ""),
                    "description": doc.get("description", ""),
                    "download_url": url,
                })

    return new_questionnaires


def download_questionnaires(questionnaires, session):
    """
    Download new consultation questionnaire Word docs to data/consultation_docs/.
    Capped at 15 per run to prevent bulk downloads on the first run.
    """
    import urllib.parse

    docs_dir = os.path.join(DATA_DIR, "consultation_docs")
    os.makedirs(docs_dir, exist_ok=True)

    downloaded = []
    for doc in questionnaires[:15]:
        url = doc.get("download_url", "")
        if not url:
            continue
        try:
            parsed = urllib.parse.urlparse(url)
            filename = urllib.parse.unquote(os.path.basename(parsed.path))
            if not filename:
                filename = f"{doc['slug']}-questionnaire.docx"
            filename = re.sub(r'[^\w\-_\. ]', '_', filename)
            filepath = os.path.join(docs_dir, filename)

            print(f"  Downloading: {filename}")
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            size_kb = round(len(resp.content) / 1024, 1)
            print(f"  Saved: {filepath} ({size_kb} KB)")
            downloaded.append({
                **doc,
                "local_path": filepath,
                "filename": filename,
                "size_kb": size_kb,
            })
            time.sleep(REQUEST_DELAY)
        except Exception as exc:
            print(f"  WARNING: Could not download {url}: {exc}")

    return downloaded

    dl_cons = download_list(new_consultation, "consultation_docs")
    dl_decs = download_list(new_decisions, "decision_docs")
    return dl_cons, dl_decs
  
# ── Consultation questionnaire detection and download ──────────────────
    print("\nChecking for new consultation questionnaires...")
    new_questionnaires = detect_new_questionnaires(stored_register, new_register)
    dl_questionnaires = []

    if new_questionnaires:
        print(f"  {len(new_questionnaires)} new questionnaire(s) detected.")
        dl_questionnaires = download_questionnaires(new_questionnaires, session)
    else:
        print("  No new questionnaires.")

    save_json(
        os.path.join(DATA_DIR, "new_documents.json"),
        {
            "run_utc": run_utc,
            "questionnaire_count": len(dl_questionnaires),
            "questionnaires": dl_questionnaires,
        },
    )
  
# ── Consultation questionnaire detection and download ──────────────────
    print("\nChecking for new consultation questionnaires...")
    new_questionnaires = detect_new_questionnaires(stored_register, new_register)
    dl_questionnaires = []

    if new_questionnaires:
        print(f"  {len(new_questionnaires)} new questionnaire(s) detected.")
        dl_questionnaires = download_questionnaires(new_questionnaires, session)
    else:
        print("  No new questionnaires.")

    save_json(
        os.path.join(DATA_DIR, "new_documents.json"),
        {
            "run_utc": run_utc,
            "questionnaire_count": len(dl_questionnaires),
            "questionnaires": dl_questionnaires,
        },
    )
  
def write_status_csv(run_utc, register, stored_register,
                     new_slugs, changed_slugs, removed_slugs, changelog):
    """Write status.csv for the Last Updated tab in Google Sheets."""
    import csv as _csv

    try:
        run_dt_utc = datetime.datetime.fromisoformat(run_utc.replace("Z", "+00:00"))
        aest_str = (run_dt_utc + datetime.timedelta(hours=10)).strftime("%d %b %Y  %H:%M")
    except Exception:
        aest_str = "Unknown"
        run_dt_utc = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

    def to_aest(ts_str):
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (dt + datetime.timedelta(hours=10)).strftime("%d %b  %H:%M")

    rows = []

    # ── Run metadata ───────────────────────────────────────────────────────
    rows += [
        ["Metric", "Value", "Notes"],
        ["", "", ""],
        ["Last scrape run (UTC)",  run_utc.replace("T", " ").replace("Z", ""), ""],
        ["Last scrape run (AEST)", aest_str, "UTC + 10 hrs (no DST adjustment)"],
        ["", "", ""],
        ["Total entries monitored",  str(len(register)),      ""],
        ["New entries this run",     str(len(new_slugs)),     ""],
        ["Changed entries this run", str(len(changed_slugs)), ""],
        ["Removed entries this run", str(len(removed_slugs)), ""],
        ["", "", ""],
        ["Auto-run schedule", "Mon to Fri", "8am / 11am / 2pm / 5pm AEST"],
        ["", "", ""],
        ["", "", ""],
    ]

    # ── Changes in this run ────────────────────────────────────────────────
    rows.append(["CHANGES IN THIS RUN", "", ""])
    rows.append(["", "", ""])

    if not new_slugs and not changed_slugs and not removed_slugs:
        rows.append(["No changes detected in this run.", "", ""])
        rows.append(["", "", ""])

    if new_slugs:
        rows.append([f"NEW ENTRIES ({len(new_slugs)})", "Case Number", "URL"])
        for slug in new_slugs:
            e = register.get(slug, {})
            rows.append([
                "+ " + e.get("title", slug),
                e.get("case_number", ""),
                e.get("url", ""),
            ])
        rows.append(["", "", ""])

    if changed_slugs:
        rows.append([
            f"CHANGED ENTRIES ({len(changed_slugs)})",
            "Field changed",
            "Old value  →  New value",
        ])
        this_run_changes = {
            c["slug"]: c["changes"]
            for c in changelog
            if c.get("timestamp_utc") == run_utc and c.get("event") == "CHANGED"
        }
        for slug in changed_slugs:
            e = register.get(slug, {})
            rows.append([
                "~ " + e.get("title", slug),
                e.get("case_number", ""),
                e.get("url", ""),
            ])
            for ch in this_run_changes.get(slug, []):
                field = ch.get("field", "").replace("_", " ").title()
                old   = str(ch.get("old", ""))[:100]
                new   = str(ch.get("new", ""))[:100]
                rows.append(["", field, f"{old}  →  {new}"])
            rows.append(["", "", ""])

    if removed_slugs:
        rows.append([f"REMOVED ENTRIES ({len(removed_slugs)})", "Case Number", ""])
        for slug in removed_slugs:
            e = stored_register.get(slug, {})
            rows.append([
                "- " + e.get("title", slug),
                e.get("case_number", ""),
                "",
            ])
        rows.append(["", "", ""])

    # ── Changes in the last 7 days ─────────────────────────────────────────
    rows.append(["", "", ""])
    rows.append(["CHANGES IN THE LAST 7 DAYS", "", ""])
    rows.append(["", "", ""])

    try:
        seven_days_ago = run_dt_utc - datetime.timedelta(days=7)

        weekly = [
            c for c in changelog
            if c.get("timestamp_utc") != run_utc
            and c.get("event") in ("NEW_ENTRY", "CHANGED", "REMOVED")
            and datetime.datetime.fromisoformat(
                c["timestamp_utc"].replace("Z", "+00:00")
            ) >= seven_days_ago
        ]

        if not weekly:
            rows.append(["No changes recorded in the last 7 days.", "", ""])
            rows.append(["", "", ""])
        else:
            weekly_new     = [c for c in weekly if c["event"] == "NEW_ENTRY"]
            weekly_changed = [c for c in weekly if c["event"] == "CHANGED"]
            weekly_removed = [c for c in weekly if c["event"] == "REMOVED"]

            if weekly_new:
                rows.append([
                    f"NEW ENTRIES ({len(weekly_new)})",
                    "Case Number", "Detected (AEST)",
                ])
                for c in weekly_new:
                    e = register.get(c["slug"], {})
                    rows.append([
                        "+ " + c.get("title", c["slug"]),
                        e.get("case_number", ""),
                        to_aest(c["timestamp_utc"]),
                    ])
                rows.append(["", "", ""])

            if weekly_changed:
                from collections import OrderedDict
                slug_events = OrderedDict()
                for c in weekly_changed:
                    slug_events.setdefault(c["slug"], []).append(c)

                rows.append([
                    f"CHANGED ENTRIES ({len(slug_events)} entries, "
                    f"{len(weekly_changed)} change events)",
                    "Field changed",
                    "Old value  →  New value  (AEST)",
                ])
                for slug, events in slug_events.items():
                    e = register.get(slug, {})
                    rows.append([
                        "~ " + events[0].get("title", slug),
                        e.get("case_number", ""),
                        e.get("url", ""),
                    ])
                    for event in events:
                        aest_ts = to_aest(event["timestamp_utc"])
                        for ch in event.get("changes", []):
                            field = ch.get("field", "").replace("_", " ").title()
                            old   = str(ch.get("old", ""))[:80]
                            new   = str(ch.get("new", ""))[:80]
                            rows.append([
                                "", field,
                                f"{old}  →  {new}  ({aest_ts})",
                            ])
                    rows.append(["", "", ""])

            if weekly_removed:
                rows.append([
                    f"REMOVED ENTRIES ({len(weekly_removed)})",
                    "Case Number", "Detected (AEST)",
                ])
                for c in weekly_removed:
                    e = stored_register.get(c["slug"], {})
                    rows.append([
                        "- " + c.get("title", c["slug"]),
                        e.get("case_number", ""),
                        to_aest(c["timestamp_utc"]),
                    ])
                rows.append(["", "", ""])

    except Exception as exc:
        rows.append([f"Error generating weekly summary: {exc}", "", ""])
        rows.append(["", "", ""])

    # Write file
    status_path = os.path.join(DATA_DIR, "status.csv")
    with open(status_path, "w", newline="", encoding="utf-8-sig") as _f:
        _csv.writer(_f).writerows(rows)
    print(f"  Status CSV written: {status_path}")
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    separator = "=" * 65
    print(f"\n{separator}")
    print(f"  ACCC Acquisitions Register Monitor")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S local')}")
    print(f"{separator}\n")

    stored_register = load_json(REGISTER_JSON, {})
    changelog = load_json(CHANGELOG_JSON, [])

    run_utc = datetime.datetime.utcnow().isoformat() + "Z"

    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Step 1: Get summary data from all listing pages ────────────────────
    listing_entries = fetch_all_listing_entries(session)

    # ── Step 2: Fetch every detail page ───────────────────────────────────
    # We must fetch detail pages for ALL entries because fields like
    # "End of determination period" and "ACCC Determination" only appear
    # on the individual case page, not the listing.
    # The article:modified_time lets us detect any change (including date
    # changes the ACCC makes without altering visible text).

    new_register = {}
    new_slugs = []
    changed_slugs = []

    total = len(listing_entries)
    print(f"\nFetching {total} detail pages...\n")

    for idx, (slug, list_entry) in enumerate(listing_entries.items(), 1):
        print(f"  [{idx:3d}/{total}] {slug[:70]}")

        detail_soup = fetch(list_entry["url"], session)
        time.sleep(REQUEST_DELAY)

        if detail_soup:
            detail = parse_detail_page(detail_soup, list_entry["url"])
        else:
            # Fall back to previously stored data so we don't lose history
            detail = stored_register.get(slug, {})
            print(f"           WARNING: using cached data for {slug}")

        # Merge: detail page values override list-page values for shared fields
        entry = {**list_entry, **detail}
        entry["slug"] = slug
        entry["last_scraped_utc"] = run_utc

        # ── Calculated day fields ──────────────────────────────────────────
        notif_date = entry.get("notification_date", "")
        end_det = entry.get("end_of_determination_period", "")
        det_pub = entry.get("determination_publication_date", "")

        # Notification date → end of determination period
        cal, biz = calc_determination_days(notif_date, end_det)
        entry["det_period_calendar_days"] = cal
        entry["det_period_business_days"] = biz

        # Notification date → determination publication date (completed cases)
        if det_pub:
            e_cal, e_biz = calc_determination_days(notif_date, det_pub)
            entry["elapsed_calendar_days"] = e_cal
            entry["elapsed_business_days"] = e_biz

        # ── Change detection ───────────────────────────────────────────────
        if slug not in stored_register:
            new_slugs.append(slug)
            changelog.append({
                "timestamp_utc": run_utc,
                "slug": slug,
                "title": entry.get("title", slug),
                "url": entry.get("url", ""),
                "event": "NEW_ENTRY",
                "changes": [],
            })
        else:
            changes = detect_changes(stored_register[slug], entry)
            if changes:
                changed_slugs.append(slug)
                changelog.append({
                    "timestamp_utc": run_utc,
                    "slug": slug,
                    "title": entry.get("title", slug),
                    "url": entry.get("url", ""),
                    "event": "CHANGED",
                    "changes": changes,
                })

        new_register[slug] = entry

    # ── Removed entries ────────────────────────────────────────────────────
    removed_slugs = sorted(set(stored_register.keys()) - set(new_register.keys()))
    for slug in removed_slugs:
        changelog.append({
            "timestamp_utc": run_utc,
            "slug": slug,
            "title": stored_register[slug].get("title", slug),
            "url": stored_register[slug].get("url", ""),
            "event": "REMOVED",
            "changes": [],
        })

    # ── Persist ────────────────────────────────────────────────────────────
    print("\nSaving data files...")
    save_json(REGISTER_JSON, new_register)
    save_json(CHANGELOG_JSON, changelog)
    export_csv(new_register)

 

    summary = generate_summary(new_register)
    save_json(SUMMARY_JSON, summary)

    latest_changes = {
        "run_utc": run_utc,
        "new_count": len(new_slugs),
        "changed_count": len(changed_slugs),
        "removed_count": len(removed_slugs),
        "new_entries": [
            {"slug": s, "title": new_register[s].get("title", s), "url": new_register[s].get("url", "")}
            for s in new_slugs
        ],
        "changed_entries": [
            {
                "slug": s,
                "title": new_register[s].get("title", s),
                "url": new_register[s].get("url", ""),
                "changes": next(
                    (c["changes"] for c in reversed(changelog) if c["slug"] == s and c["timestamp_utc"] == run_utc),
                    []
                ),
            }
            for s in changed_slugs
        ],
        "removed_entries": [
            {"slug": s, "title": stored_register[s].get("title", s)}
            for s in removed_slugs
        ],
    }
    save_json(LATEST_CHANGES_JSON, latest_changes)

    # ── Summary output ─────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"  Run complete at {run_utc}")
    print(f"  Total entries:   {len(new_register)}")
    print(f"  New entries:     {len(new_slugs)}")
    print(f"  Changed entries: {len(changed_slugs)}")
    print(f"  Removed entries: {len(removed_slugs)}")
    print(f"{'─' * 65}")

    if new_slugs:
        print("\n  NEW:")
        for s in new_slugs:
            print(f"    + {new_register[s].get('title', s)}")

    if changed_slugs:
        print("\n  CHANGED:")
        for s in changed_slugs:
            title = new_register[s].get("title", s)
            this_run_changes = [
                c for c in changelog
                if c["slug"] == s and c["timestamp_utc"] == run_utc
            ]
            print(f"    ~ {title}")
            if this_run_changes:
                for ch in this_run_changes[0].get("changes", []):
                    print(f"        {ch['field']}: '{ch['old']}' → '{ch['new']}'")

    if removed_slugs:
        print("\n  REMOVED:")
        for s in removed_slugs:
            print(f"    - {stored_register[s].get('title', s)}")

    if summary.get("decisions_due_within_10_days"):
        print("\n  DECISIONS DUE WITHIN 10 DAYS:")
        for item in summary["decisions_due_within_10_days"]:
            print(f"    ⚠  {item['title']} — {item['end_of_determination_period']} ({item['days_remaining']}d)")

 # ── GitHub Actions output variables ────────────────────────────────────
    has_changes = bool(new_slugs or changed_slugs or removed_slugs)
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_changes={'true' if has_changes else 'false'}\n")
            f.write(f"new_count={len(new_slugs)}\n")
            f.write(f"changed_count={len(changed_slugs)}\n")
            f.write(f"removed_count={len(removed_slugs)}\n")
            f.write(f"consultation_doc_count={len(dl_questionnaires)}\n")

  # ── New document detection and download ────────────────────────────────
    print("\nChecking for new documents...")
    new_consultation, new_decisions = detect_new_documents(
        stored_register, new_register
    )
    dl_cons, dl_decs = [], []

    if new_consultation or new_decisions:
        print(f"  {len(new_consultation)} new consultation doc(s), "
              f"{len(new_decisions)} new decision doc(s).")
        dl_cons, dl_decs = download_new_documents(
            new_consultation, new_decisions, session
        )
    else:
        print("  No new documents.")

    save_json(
        os.path.join(DATA_DIR, "new_documents.json"),
        {
            "run_utc": run_utc,
            "consultation_count": len(dl_cons),
            "decision_count": len(dl_decs),
            "consultation_docs": dl_cons,
            "decision_docs": dl_decs,
        },
    )
  
    write_status_csv(
        run_utc, new_register, stored_register,
        new_slugs, changed_slugs, removed_slugs, changelog
    )

    return has_changes


if __name__ == "__main__":
    run()
