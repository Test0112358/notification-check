#!/usr/bin/env python3
"""
ACCC Register Verifier
======================
Three independent verification layers that check the accuracy of scraped data
without relying on the same code paths as the original scraper.

Layer 1 — Internal consistency rules (no HTTP, runs on all entries):
    Every entry is checked against rules that are true by definition.
    Examples: dates must be in logical order, case numbers must match the
    entry type, required fields must be present, business day counts must
    re-verify independently from scratch.

Layer 2 — Cross-source check (re-fetches listing pages, ~6 HTTP requests):
    The register listing pages show a subset of fields (acquisition_status,
    type, case_number, stage, notification_date). These are re-scraped fresh
    and compared against what is stored from the detail-page scrape. If the
    listing says "Phase 1" but the stored detail page says "Phase 2", one of
    them is wrong.

Layer 3 — Detail page re-scrape (re-fetches a random sample of case pages):
    A random sample of detail pages is fetched independently, parsed using a
    completely separate extraction method (CSS selectors and regex, NOT the
    h3-navigation used in scraper.py), and the results are compared field by
    field against stored data. Any discrepancy surfaces a parser bug, a
    network error from the original scrape, or a page change that was missed.

Outputs:
    data/verification_report.json  — full structured issue list
    data/verification_report.csv   — spreadsheet-ready, one row per issue
    data/verification_summary.json — pass/fail counts for dashboards/alerts
"""

import csv
import json
import os
import random
import re
import sys
import time
import datetime
import hashlib
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from workalendar.oceania import AustralianCapitalTerritory

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://www.accc.gov.au"
REGISTER_PATH = "/public-registers/mergers-and-acquisitions-registers/acquisitions-register"
REGISTER_URL = BASE_URL + REGISTER_PATH

DATA_DIR = "data"
REGISTER_JSON = os.path.join(DATA_DIR, "register.json")
REPORT_JSON = os.path.join(DATA_DIR, "verification_report.json")
REPORT_CSV = os.path.join(DATA_DIR, "verification_report.csv")
SUMMARY_JSON = os.path.join(DATA_DIR, "verification_summary.json")

# How many detail pages to re-scrape in Layer 3.
# Set to 0 to skip Layer 3 entirely (faster, useful on every-run checks).
# Default: re-scrape at least 15 entries, or 15% of the register, whichever is larger.
SAMPLE_MIN = 15
SAMPLE_PCT = 0.15

REQUEST_DELAY = 1.2
REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "ACCC-Register-Verifier/1.0 "
        "(automated accuracy check; github.com/Test0112358/accc-monitor)"
    )
}

ACT_CAL = AustralianCapitalTerritory()

DATE_FORMATS = [
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%Y-%m-%d",
]

# Known valid values for categorical fields
VALID_ACQUISITION_STATUS = {
    "Under assessment",
    "Assessment completed",
    "Assessment ceased",
    "Assessment suspended",
}

VALID_TYPES = {"Notification", "Waiver"}

VALID_STAGES = {
    "Phase 1 - initial assessment",
    "Phase 2 - detailed assessment",
    "Public Benefit Phase",
    "Waiver application",
}

VALID_DETERMINATIONS = {
    "Approved",
    "Approved with conditions",
    "Not approved",
    "Not applicable",
}

# Required fields: every entry must have these
REQUIRED_FIELDS = ["title", "url", "case_number", "type", "notification_date"]

# Severity levels
ERROR = "ERROR"      # Data is definitely wrong
WARNING = "WARNING"  # Data may be wrong or unexpected
INFO = "INFO"        # Informational anomaly worth noting


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip())


def issue(slug, title, url, layer, rule, severity, detail, field="", stored="", expected=""):
    """Create a standardised verification issue record."""
    return {
        "slug": slug,
        "title": title,
        "url": url,
        "layer": layer,
        "rule": rule,
        "severity": severity,
        "detail": detail,
        "field": field,
        "stored_value": clean(str(stored)),
        "expected_or_found": clean(str(expected)),
    }


def fetch_soup(url, session):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml"), resp.text
        except requests.RequestException as exc:
            wait = REQUEST_DELAY * (2 ** attempt)
            print(f"      Attempt {attempt+1}/3 failed: {exc}. Waiting {wait:.1f}s...")
            if attempt < 2:
                time.sleep(wait)
    return None, None


# ---------------------------------------------------------------------------
# Layer 1: Internal consistency rules (no HTTP)
# ---------------------------------------------------------------------------

def recalculate_business_days(start_str, end_str):
    """
    Re-calculate ACT business days independently from the stored value.
    Uses a direct day-by-day count as a second opinion on workalendar's output.
    """
    start = parse_date(start_str)
    end = parse_date(end_str)
    if not start or not end or end < start:
        return None
    count = 0
    current = start
    while current <= end:
        if ACT_CAL.is_working_day(current):
            count += 1
        current += datetime.timedelta(days=1)
    return count


def layer1_consistency(register):
    """
    Check all ~250 entries against rules that are true by definition.
    No HTTP requests. Catches parser bugs, empty fields, logical errors.
    """
    print("\n  Layer 1: Internal consistency rules...")
    issues = []

    for slug, e in register.items():
        t = e.get("title", slug)
        u = e.get("url", "")
        L = 1

        def add(rule, sev, detail, field="", stored="", expected=""):
            issues.append(issue(slug, t, u, L, rule, sev, detail, field, stored, expected))

        # ── Required fields ────────────────────────────────────────────────
        for field in REQUIRED_FIELDS:
            val = clean(e.get(field, ""))
            if not val:
                add(
                    "REQUIRED_FIELD_EMPTY",
                    ERROR,
                    f"Required field '{field}' is missing or empty.",
                    field, val, "non-empty string",
                )

        # ── Case number format ─────────────────────────────────────────────
        case_num = clean(e.get("case_number", ""))
        entry_type = clean(e.get("type", ""))

        if entry_type == "Notification" and case_num:
            if not re.match(r"^MN-\d+$", case_num):
                add(
                    "CASE_NUMBER_FORMAT",
                    ERROR,
                    f"Notification entry has case number '{case_num}' — expected MN-NNNNN format.",
                    "case_number", case_num, "MN-NNNNN",
                )

        if entry_type == "Waiver" and case_num:
            if not re.match(r"^WA-\d+$", case_num):
                add(
                    "CASE_NUMBER_FORMAT",
                    ERROR,
                    f"Waiver entry has case number '{case_num}' — expected WA-NNNNN format.",
                    "case_number", case_num, "WA-NNNNN",
                )

        # ── Categorical field validation ───────────────────────────────────
        acq_status = clean(e.get("acquisition_status", ""))
        if acq_status and acq_status not in VALID_ACQUISITION_STATUS:
            add(
                "UNKNOWN_STATUS_VALUE",
                WARNING,
                f"Unrecognised acquisition_status: '{acq_status}'.",
                "acquisition_status", acq_status, str(VALID_ACQUISITION_STATUS),
            )

        if entry_type and entry_type not in VALID_TYPES:
            add(
                "UNKNOWN_TYPE_VALUE",
                WARNING,
                f"Unrecognised type: '{entry_type}'.",
                "type", entry_type, str(VALID_TYPES),
            )

        stage = clean(e.get("stage", ""))
        if stage and stage not in VALID_STAGES:
            add(
                "UNKNOWN_STAGE_VALUE",
                WARNING,
                f"Unrecognised stage: '{stage}'.",
                "stage", stage, str(VALID_STAGES),
            )

        det = clean(e.get("accc_determination", ""))
        if det and det not in VALID_DETERMINATIONS:
            add(
                "UNKNOWN_DETERMINATION_VALUE",
                WARNING,
                f"Unrecognised ACCC determination: '{det}'.",
                "accc_determination", det, str(VALID_DETERMINATIONS),
            )

        # ── Waiver-type stage consistency ─────────────────────────────────
        if entry_type == "Waiver" and stage and stage != "Waiver application":
            add(
                "WAIVER_STAGE_MISMATCH",
                WARNING,
                f"Waiver entry has stage '{stage}' — expected 'Waiver application'.",
                "stage", stage, "Waiver application",
            )

        # ── Date field parseability ────────────────────────────────────────
        notif_str = clean(e.get("notification_date", ""))
        end_str = clean(e.get("end_of_determination_period", ""))
        pub_str = clean(e.get("determination_publication_date", ""))

        notif_date = parse_date(notif_str)
        end_date = parse_date(end_str)
        pub_date = parse_date(pub_str)

        if notif_str and not notif_date:
            add(
                "DATE_UNPARSEABLE",
                ERROR,
                f"Cannot parse notification_date: '{notif_str}'.",
                "notification_date", notif_str, "parseable date",
            )

        if end_str and not end_date:
            add(
                "DATE_UNPARSEABLE",
                ERROR,
                f"Cannot parse end_of_determination_period: '{end_str}'.",
                "end_of_determination_period", end_str, "parseable date",
            )

        if pub_str and not pub_date:
            add(
                "DATE_UNPARSEABLE",
                ERROR,
                f"Cannot parse determination_publication_date: '{pub_str}'.",
                "determination_publication_date", pub_str, "parseable date",
            )

        # ── Date ordering ──────────────────────────────────────────────────
        if notif_date and end_date:
            if end_date <= notif_date:
                add(
                    "DATE_ORDER_INVALID",
                    ERROR,
                    f"end_of_determination_period ({end_str}) is not after notification_date ({notif_str}).",
                    "end_of_determination_period", end_str, f"after {notif_str}",
                )

        if notif_date and pub_date:
            if pub_date < notif_date:
                add(
                    "DATE_ORDER_INVALID",
                    ERROR,
                    f"determination_publication_date ({pub_str}) is before notification_date ({notif_str}).",
                    "determination_publication_date", pub_str, f"on or after {notif_str}",
                )

        # ── Plausibility: date is not in the distant future or past ────────
        today = datetime.date.today()
        REGIME_START = datetime.date(2025, 7, 1)  # New merger regime from July 2025

        if notif_date:
            if notif_date < REGIME_START:
                add(
                    "DATE_BEFORE_REGIME",
                    WARNING,
                    f"notification_date ({notif_str}) is before the new merger regime start (1 Jul 2025).",
                    "notification_date", notif_str, f"on or after {REGIME_START}",
                )
            if notif_date > today + datetime.timedelta(days=7):
                add(
                    "DATE_IN_FUTURE",
                    WARNING,
                    f"notification_date ({notif_str}) is more than 7 days in the future.",
                    "notification_date", notif_str, f"on or before ~{today}",
                )

        # ── Business day calculation re-verification ───────────────────────
        if notif_date and end_date:
            stored_biz = e.get("det_period_business_days")
            stored_cal = e.get("det_period_calendar_days")

            recalc_cal = (end_date - notif_date).days
            recalc_biz = recalculate_business_days(notif_str, end_str)

            if stored_cal is not None and stored_cal != recalc_cal:
                add(
                    "CALENDAR_DAYS_MISMATCH",
                    ERROR,
                    f"Stored calendar days ({stored_cal}) does not match independent recalculation ({recalc_cal}).",
                    "det_period_calendar_days", str(stored_cal), str(recalc_cal),
                )

            if recalc_biz is not None and stored_biz is not None:
                if abs(int(stored_biz) - recalc_biz) > 1:
                    # Allow 1-day tolerance for boundary counting conventions
                    add(
                        "BUSINESS_DAYS_MISMATCH",
                        ERROR,
                        f"Stored ACT business days ({stored_biz}) differs by more than 1 from independent recalculation ({recalc_biz}).",
                        "det_period_business_days", str(stored_biz), str(recalc_biz),
                    )

        # ── Completed case completeness ────────────────────────────────────
        if acq_status == "Assessment completed":
            if entry_type == "Notification" and not det:
                add(
                    "COMPLETED_MISSING_DETERMINATION",
                    WARNING,
                    "Assessment completed but accc_determination is empty.",
                    "accc_determination", "", "non-empty",
                )
            if not pub_str:
                add(
                    "COMPLETED_MISSING_PUB_DATE",
                    WARNING,
                    "Assessment completed but determination_publication_date is empty.",
                    "determination_publication_date", "", "non-empty",
                )

        # ── Active case completeness ───────────────────────────────────────
        if acq_status == "Under assessment" and entry_type == "Notification":
            if not stage:
                add(
                    "ACTIVE_MISSING_STAGE",
                    WARNING,
                    "Under assessment notification has no stage.",
                    "stage", "", "non-empty",
                )
            if not end_str:
                add(
                    "ACTIVE_MISSING_END_DATE",
                    INFO,
                    "Under assessment notification has no end_of_determination_period. "
                    "This may be normal if the entry was very recently added.",
                    "end_of_determination_period", "", "non-empty (expected within 1 business day of notification)",
                )

        # ── Acquirers and targets not both empty ──────────────────────────
        if not clean(e.get("acquirers", "")) and not clean(e.get("targets", "")):
            add(
                "PARTIES_BOTH_EMPTY",
                WARNING,
                "Both acquirers and targets are empty.",
                "acquirers", "", "at least one non-empty",
            )

        # ── ANZSIC code format ─────────────────────────────────────────────
        anzsic = clean(e.get("anzsic_codes", ""))
        if anzsic:
            codes = re.findall(r"\b\d{4}\b", anzsic)
            if not codes:
                add(
                    "ANZSIC_NO_CODE_FOUND",
                    WARNING,
                    f"anzsic_codes field present but no 4-digit ANZSIC codes found: '{anzsic[:80]}'.",
                    "anzsic_codes", anzsic, "at least one 4-digit code",
                )

        # ── ACCC page last_modified plausibility ───────────────────────────
        mod_str = clean(e.get("last_modified_accc", ""))
        if not mod_str:
            add(
                "MISSING_MODIFIED_TIMESTAMP",
                WARNING,
                "last_modified_accc (article:modified_time) is empty — scraper may have failed to read meta tag.",
                "last_modified_accc", "", "ISO 8601 timestamp",
            )

    print(f"    Checked {len(register)} entries. Found {len(issues)} issues.")
    return issues


# ---------------------------------------------------------------------------
# Layer 2: Cross-source check (re-fetch listing pages)
# ---------------------------------------------------------------------------

def parse_listing_for_verification(soup):
    """
    Re-extract the subset of fields that appear on the listing page.
    Uses a completely different approach from scraper.py:
    raw text pattern matching on the card text, no h3 navigation.
    Returns a dict keyed by slug.
    """
    results = {}
    REGISTER_SUBPATH = REGISTER_PATH + "/"

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(REGISTER_SUBPATH):
            continue
        if "?" in href or "#" in href:
            continue

        slug = href.rstrip("/").split("/")[-1]
        text = a.get_text(separator="|SEP|")

        def extract(pattern, text=text):
            m = re.search(pattern, text, re.IGNORECASE)
            return clean(m.group(1)) if m else ""

        # Use regex on the raw concatenated card text — entirely different
        # extraction method from scraper.py's line-by-line approach.
        results[slug] = {
            "acquisition_status": extract(
                r"Acquisition status\|SEP\|(.*?)\|SEP\|"
            ) or extract(r"status\|SEP\|(Under assessment|Assessment completed|Assessment ceased|Assessment suspended)"),
            "type": extract(
                r"\|SEP\|Type\|SEP\|(Notification|Waiver)\|SEP\|"
            ),
            "case_number": extract(
                r"Case number\|SEP\|((?:MN|WA)-\d+)"
            ),
            "stage": extract(
                r"Stage\|SEP\|(.*?)\|SEP\|"
            ),
            "notification_date": extract(
                r"(?:Effective notification date|Waiver application date|Notification / Application date)\|SEP\|(.*?)\|SEP\|"
            ) or extract(
                r"(?:Effective notification date|Waiver application date|Notification / Application date)\|SEP\|(.*?)$"
            ),
        }

    return results


def layer2_cross_source(register, session):
    """
    Re-fetch all listing pages and compare extracted values against stored data.
    Catches cases where the detail-page scraper extracted a different value than
    what the listing page shows — a reliable sign of a parser error.
    """
    print("\n  Layer 2: Cross-source check (re-fetching listing pages)...")
    issues = []

    # Fetch all listing pages fresh
    fresh_listing = {}
    page = 0
    while True:
        url = f"{REGISTER_URL}?items_per_page=50&page={page}"
        print(f"    Fetching listing page {page + 1}...")
        soup, _ = fetch_soup(url, session)
        if not soup:
            issues.append(issue(
                "REGISTER_INDEX", "Register index page", url, 2,
                "LISTING_PAGE_FETCH_FAILED", ERROR,
                f"Could not fetch listing page {page + 1}. Cross-source check may be incomplete.",
            ))
            break
        page_data = parse_listing_for_verification(soup)
        fresh_listing.update(page_data)
        time.sleep(REQUEST_DELAY)

        # Check if there's a next page
        next_link = soup.find("a", title=re.compile(r"Go to next page", re.I))
        if not next_link:
            break
        page += 1

    print(f"    Re-scraped {len(fresh_listing)} entries from listing pages.")

    # Compare shared fields
    SHARED_FIELDS = [
        ("acquisition_status", "acquisition_status"),
        ("type", "type"),
        ("case_number", "case_number"),
        ("stage", "stage"),
        ("notification_date", "notification_date"),
    ]

    matched = 0
    for slug, fresh in fresh_listing.items():
        if slug not in register:
            issues.append(issue(
                slug, slug, f"{BASE_URL}{REGISTER_PATH}/{slug}", 2,
                "ENTRY_IN_LISTING_NOT_IN_STORED",
                ERROR,
                "Entry appears on the live listing page but is missing from stored register.json. "
                "The previous scrape may have failed to process this entry.",
            ))
            continue

        stored = register[slug]
        t = stored.get("title", slug)
        u = stored.get("url", "")

        for fresh_field, stored_field in SHARED_FIELDS:
            fv = clean(fresh.get(fresh_field, ""))
            sv = clean(stored.get(stored_field, ""))
            if fv and sv and fv != sv:
                issues.append(issue(
                    slug, t, u, 2,
                    "CROSS_SOURCE_MISMATCH",
                    ERROR,
                    f"Field '{stored_field}' disagrees between listing page ('{fv}') "
                    f"and stored detail-page data ('{sv}').",
                    stored_field, sv, fv,
                ))
            elif fv and not sv:
                issues.append(issue(
                    slug, t, u, 2,
                    "CROSS_SOURCE_STORED_EMPTY",
                    WARNING,
                    f"Listing page has '{stored_field}' = '{fv}' but stored data has it empty.",
                    stored_field, sv, fv,
                ))
            else:
                matched += 1

    # Check for stored entries that no longer appear in the live listing
    stored_but_not_live = set(register.keys()) - set(fresh_listing.keys())
    for slug in stored_but_not_live:
        stored = register[slug]
        issues.append(issue(
            slug, stored.get("title", slug), stored.get("url", ""), 2,
            "ENTRY_STORED_BUT_NOT_IN_LISTING",
            WARNING,
            "Entry is in stored data but did not appear on any listing page in this verification run. "
            "It may have been removed from the register, or the listing scrape may be incomplete.",
        ))

    print(f"    {matched} field values matched. {len([i for i in issues if i['layer'] == 2])} issues found.")
    return issues


# ---------------------------------------------------------------------------
# Layer 3: Detail page re-scrape (independent parser, random sample)
# ---------------------------------------------------------------------------

def extract_field_alternative(soup, label_pattern):
    """
    Alternative extraction: find a label using a regex on ALL text content,
    then return the text of the next non-empty sibling element.
    Completely different code path from scraper.py.
    """
    # Strategy: find any tag whose text matches the label, then walk siblings
    for tag in soup.find_all(True):
        if re.fullmatch(label_pattern, clean(tag.get_text()), re.IGNORECASE):
            sib = tag.next_sibling
            while sib is not None:
                if hasattr(sib, "get_text"):
                    text = clean(sib.get_text())
                    if text and text.lower() != clean(tag.get_text()).lower():
                        return text
                elif isinstance(sib, str):
                    text = sib.strip()
                    if text:
                        return text
                sib = sib.next_sibling
    return ""


def re_parse_detail_independently(soup, url):
    """
    Re-parse a detail page using CSS selectors and regex — NOT the h3-navigation
    approach from scraper.py. This serves as an independent second opinion.
    Returns a dict of field → value.
    """
    if not soup:
        return {}

    result = {}

    # ── Title from h1 ──────────────────────────────────────────────────────
    h1 = soup.find("h1")
    if h1:
        result["title"] = clean(h1.get_text())

    # ── article:modified_time from meta ────────────────────────────────────
    meta = soup.find("meta", {"property": "article:modified_time"})
    if meta:
        result["last_modified_accc"] = clean(meta.get("content", ""))

    # ── Full page text for regex extraction ───────────────────────────────
    # Join all text with a sentinel separator so we can use word-boundary regex.
    all_text = soup.get_text(separator=" ||| ")

    def regex_extract(pattern):
        m = re.search(pattern, all_text, re.IGNORECASE)
        return clean(m.group(1)) if m else ""

    # ── Scalar fields via regex on full page text ──────────────────────────
    result["acquisition_status"] = regex_extract(
        r"Acquisition status\s*\|\|\|\s*(Under assessment|Assessment completed|Assessment ceased|Assessment suspended)"
    )
    result["case_number"] = regex_extract(
        r"Acquisition case number\s*\|\|\|\s*((?:MN|WA)-\d+)"
    )
    result["type"] = regex_extract(
        r"\|\|\|\s*Type\s*\|\|\|\s*(Notification|Waiver)\s*\|\|\|"
    )
    result["stage"] = regex_extract(
        r"\|\|\|\s*Stage\s*\|\|\|\s*(Phase [12][^|]+?|Public Benefit Phase|Waiver application)\s*\|\|\|"
    )
    result["notification_date"] = (
        regex_extract(
            r"(?:Effective notification date|Waiver application date)\s*\|\|\|\s*([\d]+ \w+ \d{4})"
        )
    )
    result["end_of_determination_period"] = regex_extract(
        r"End of determination period\s*\|\|\|\s*([\d]+ \w+ \d{4})"
    )
    result["accc_determination"] = regex_extract(
        r"ACCC [Dd]etermination\s*\|\|\|\s*(Approved(?:\s+with\s+conditions)?|Not approved|Not applicable)"
    )
    result["determination_publication_date"] = regex_extract(
        r"[Dd]etermination publication date\s*\|\|\|\s*([\d]+ \w+ \d{4})"
    )

    # ── Acquirers: extract all li items after the "Acquirer" heading ───────
    acquirer_section = soup.find(
        lambda t: t.name in ("h3", "h2") and "acquirer" in t.get_text().lower()
    )
    if acquirer_section:
        ul = acquirer_section.find_next("ul")
        if ul:
            result["acquirers"] = "; ".join(
                clean(li.get_text()) for li in ul.find_all("li") if clean(li.get_text())
            )

    # ── Targets ────────────────────────────────────────────────────────────
    target_section = soup.find(
        lambda t: t.name in ("h3", "h2") and "target" in t.get_text().lower()
    )
    if target_section:
        ul = target_section.find_next("ul")
        if ul:
            result["targets"] = "; ".join(
                clean(li.get_text()) for li in ul.find_all("li") if clean(li.get_text())
            )

    # ── ANZSIC codes ───────────────────────────────────────────────────────
    result["anzsic_codes"] = regex_extract(
        r"ANZSIC code\(s\)\s*\|\|\|\s*([\d].+?)\s*\|\|\|"
    )

    # ── Content fingerprint (SHA256 of the meaningful text) ───────────────
    # Stable fingerprint — excludes dynamic elements like nonces, session tokens.
    # Used to detect any content change that was missed by field parsing.
    body = soup.find("main") or soup.find("body")
    if body:
        # Remove script/style/nav/footer from fingerprint scope
        for tag in body.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        fingerprint_text = re.sub(r"\s+", " ", body.get_text()).strip()
        result["_content_fingerprint"] = hashlib.sha256(
            fingerprint_text.encode("utf-8")
        ).hexdigest()[:16]

    return result


FIELDS_TO_COMPARE_IN_RESCRAPE = [
    "title",
    "acquisition_status",
    "type",
    "case_number",
    "stage",
    "notification_date",
    "end_of_determination_period",
    "accc_determination",
    "determination_publication_date",
    "last_modified_accc",
]


def layer3_rescrape(register, session, sample_size=None):
    """
    Re-fetch a random sample of detail pages, parse them with an independent
    method, and compare field by field against stored data.
    """
    total = len(register)
    if sample_size is None:
        sample_size = max(SAMPLE_MIN, int(total * SAMPLE_PCT))
    sample_size = min(sample_size, total)

    slugs = random.sample(list(register.keys()), sample_size)
    print(f"\n  Layer 3: Detail page re-scrape (random sample of {sample_size}/{total})...")

    issues = []
    for i, slug in enumerate(slugs, 1):
        stored = register[slug]
        url = stored.get("url", "")
        t = stored.get("title", slug)

        print(f"    [{i:3d}/{sample_size}] {slug[:65]}")

        soup, raw_html = fetch_soup(url, session)
        time.sleep(REQUEST_DELAY)

        if not soup:
            issues.append(issue(
                slug, t, url, 3,
                "RESCRAPE_FETCH_FAILED",
                ERROR,
                "Could not fetch this page during verification re-scrape. "
                "Original scrape data may be stale or the page is temporarily unavailable.",
            ))
            continue

        fresh = re_parse_detail_independently(soup, url)

        # ── Compare each field ─────────────────────────────────────────────
        for field in FIELDS_TO_COMPARE_IN_RESCRAPE:
            sv = clean(str(stored.get(field, "") or ""))
            fv = clean(str(fresh.get(field, "") or ""))

            # Skip if the independent parser could not extract the field
            # (empty result from re-parser is not necessarily an error)
            if not fv:
                continue

            if sv != fv:
                issues.append(issue(
                    slug, t, url, 3,
                    "RESCRAPE_VALUE_MISMATCH",
                    ERROR,
                    f"Field '{field}': stored='{sv}', independent re-scrape='{fv}'. "
                    "One of the two is wrong, or the page changed since the last scrape.",
                    field, sv, fv,
                ))

        # ── Check if stored last_modified matches re-scraped last_modified ─
        # If they differ, the page was updated between the original scrape and now.
        stored_mod = clean(stored.get("last_modified_accc", ""))
        fresh_mod = clean(fresh.get("last_modified_accc", ""))
        if stored_mod and fresh_mod and stored_mod != fresh_mod:
            issues.append(issue(
                slug, t, url, 3,
                "PAGE_MODIFIED_SINCE_SCRAPE",
                WARNING,
                f"Page was modified after the last scrape. "
                f"Scraped at modified_time={stored_mod}, current modified_time={fresh_mod}. "
                "The next scheduled scrape will pick up these changes automatically.",
                "last_modified_accc", stored_mod, fresh_mod,
            ))

    print(f"    Re-scrape complete. {len([i for i in issues if i['layer'] == 3])} issues found.")
    return issues


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def categorise_issues(issues):
    errors = [i for i in issues if i["severity"] == ERROR]
    warnings = [i for i in issues if i["severity"] == WARNING]
    infos = [i for i in issues if i["severity"] == INFO]
    return errors, warnings, infos


def save_report(issues):
    os.makedirs(DATA_DIR, exist_ok=True)

    # JSON
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(issues, f, indent=2, ensure_ascii=False)

    # CSV (one row per issue, easy to filter in Excel/Sheets)
    if issues:
        fieldnames = list(issues[0].keys())
        with open(REPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(issues)
    else:
        with open(REPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            f.write("No issues found.\n")

    print(f"  Report saved: {REPORT_JSON}, {REPORT_CSV}")


def save_summary(issues, register_size, run_utc, sample_size):
    errors, warnings, infos = categorise_issues(issues)

    by_rule = {}
    for iss in issues:
        rule = iss["rule"]
        by_rule[rule] = by_rule.get(rule, 0) + 1

    summary = {
        "run_utc": run_utc,
        "register_entries_checked": register_size,
        "layer3_sample_size": sample_size,
        "total_issues": len(issues),
        "errors": len(errors),
        "warnings": len(warnings),
        "infos": len(infos),
        "passed": len(errors) == 0,
        "issues_by_rule": by_rule,
        "error_entries": [
            {"slug": i["slug"], "title": i["title"], "rule": i["rule"], "detail": i["detail"]}
            for i in errors
        ],
    }

    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(skip_layer3=False, sample_size=None, seed=None):
    separator = "=" * 65
    print(f"\n{separator}")
    print(f"  ACCC Register Verifier")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S local')}")
    print(f"{separator}\n")

    if not os.path.exists(REGISTER_JSON):
        print(f"ERROR: {REGISTER_JSON} not found. Run scraper.py first.")
        sys.exit(1)

    with open(REGISTER_JSON, "r", encoding="utf-8") as f:
        register = json.load(f)

    print(f"  Loaded {len(register)} entries from {REGISTER_JSON}.")

    run_utc = datetime.datetime.utcnow().isoformat() + "Z"

    if seed is not None:
        random.seed(seed)

    all_issues = []

    # Layer 1
    all_issues.extend(layer1_consistency(register))

    # Layers 2 and 3 require HTTP
    session = requests.Session()
    session.headers.update(HEADERS)

    # Layer 2
    all_issues.extend(layer2_cross_source(register, session))

    # Layer 3
    actual_sample = 0
    if not skip_layer3:
        actual_sample = sample_size or max(SAMPLE_MIN, int(len(register) * SAMPLE_PCT))
        actual_sample = min(actual_sample, len(register))
        all_issues.extend(layer3_rescrape(register, session, actual_sample))

    # Save outputs
    print("\n  Saving outputs...")
    save_report(all_issues)
    summary = save_summary(all_issues, len(register), run_utc, actual_sample)

    # Print summary
    errors, warnings, infos = categorise_issues(all_issues)
    print(f"\n{'─' * 65}")
    print(f"  Verification complete.")
    print(f"  Register entries checked: {len(register)}")
    print(f"  Layer 3 sample size:      {actual_sample}")
    print(f"  Total issues:             {len(all_issues)}")
    print(f"    Errors:   {len(errors)}")
    print(f"    Warnings: {len(warnings)}")
    print(f"    Info:     {len(infos)}")
    print(f"  Result: {'✅ PASSED' if not errors else '❌ FAILED — see data/verification_report.csv'}")
    print(f"{'─' * 65}\n")

    if errors:
        print("  ERRORS:")
        for e in errors[:20]:
            print(f"    [{e['rule']}] {e['title']}: {e['detail']}")
        if len(errors) > 20:
            print(f"    ... and {len(errors) - 20} more errors in the report.")

    # GitHub Actions outputs
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"verification_passed={'true' if not errors else 'false'}\n")
            f.write(f"error_count={len(errors)}\n")
            f.write(f"warning_count={len(warnings)}\n")

    return not errors  # True = passed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-layer3", action="store_true",
                        help="Skip the detail page re-scrape (faster, for frequent runs)")
    parser.add_argument("--sample", type=int, default=None,
                        help="Override number of entries to re-scrape in Layer 3")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible Layer 3 sample selection")
    args = parser.parse_args()

    passed = run(
        skip_layer3=args.skip_layer3,
        sample_size=args.sample,
        seed=args.seed,
    )
    sys.exit(0 if passed else 1)
