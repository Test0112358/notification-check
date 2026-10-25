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
    re-verify exactly using a different counting method (numpy.busday_count).

Layer 2 — Cross-source check (listing pages saved by this run's scrape):
    The register listing pages show a subset of fields (acquisition_status,
    type, case_number, stage, notification_date). These are re-extracted with
    a regex parser and compared against what is stored from the detail-page
    scrape. If the listing says "Phase 1" but the stored detail page says
    "Phase 2", one of them is wrong.

Layer 3 — Detail page re-parse (case pages saved by this run's scrape):
    A sample of detail pages is parsed using a completely separate extraction
    method (CSS selectors and regex, NOT the h3-navigation used in scraper.py),
    and the results are compared field by field against stored data. Any
    discrepancy surfaces a parser bug.

Layers 2 and 3 read the HTML that scraper.py saved under .cache/ during the
same workflow run, so verification adds no extra requests to the ACCC site.
Use --live N to additionally fetch N random pages live (e.g. when running
locally without a cache).

Outputs:
    data/verification_report.json  — full structured issue list
    data/verification_report.csv   — spreadsheet-ready, one row per issue
    data/verification_summary.json — pass/fail counts for dashboards/alerts
"""

import csv
import datetime
import hashlib
import json
import os
import random
import re
import sys

import numpy as np
from bs4 import BeautifulSoup

from accc_common import (
    ACT_PUBLIC_HOLIDAYS, BASE_URL, CASE_PATH_MARKER, DATA_DIR, DOCUMENTS_MANIFEST_JSON,
    HOLIDAY_TABLE_LAST_YEAR, REGISTER_JSON, REGISTER_PATH, REGISTER_URL, AccessBlocked,
    PoliteSession, cached_detail_html, cached_listing_pages, in_christmas_suspension,
    load_json, parse_date, utc_now_iso, write_github_outputs,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPORT_JSON = os.path.join(DATA_DIR, "verification_report.json")
REPORT_CSV = os.path.join(DATA_DIR, "verification_report.csv")
SUMMARY_JSON = os.path.join(DATA_DIR, "verification_summary.json")

# How many cached detail pages to re-parse in Layer 3 (all of them if fewer).
SAMPLE_MIN = 40
SAMPLE_PCT = 0.25

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

def clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ").strip())


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


# ---------------------------------------------------------------------------
# Layer 1: Internal consistency rules (no HTTP)
# ---------------------------------------------------------------------------

def _non_business_dates(start, end):
    """Holidays and Christmas suspension days in [start, end], as numpy dates."""
    days = set(d for d in ACT_PUBLIC_HOLIDAYS if start <= d <= end)
    current = start
    while current <= end:
        if in_christmas_suspension(current):
            days.add(current)
        current += datetime.timedelta(days=1)
    return np.array(sorted(days), dtype="datetime64[D]")


def recalculate_business_days(start_str, end_str):
    """
    Independent recalculation of ACCC business days for verification.
    Uses numpy.busday_count (a different algorithm from the scraper's
    day-by-day loop) over the half-open range (start, end], i.e. Day 0 is the
    notification date and the end date is counted.
    """
    start = parse_date(start_str)
    end = parse_date(end_str)
    if not start or not end or end < start:
        return None
    begin = np.datetime64(start + datetime.timedelta(days=1), "D")
    stop = np.datetime64(end + datetime.timedelta(days=1), "D")
    return int(np.busday_count(begin, stop, weekmask="1111100",
                               holidays=_non_business_dates(start, end)))


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
                    f"Notification entry has case number '{case_num}'; expected MN-NNNNN format.",
                    "case_number", case_num, "MN-NNNNN",
                )

        if entry_type == "Waiver" and case_num:
            if not re.match(r"^WA-\d+$", case_num):
                add(
                    "CASE_NUMBER_FORMAT",
                    ERROR,
                    f"Waiver entry has case number '{case_num}'; expected WA-NNNNN format.",
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
                f"Waiver entry has stage '{stage}'; expected 'Waiver application'.",
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
                # Exact match: both sides use the same Day 0 convention, so any
                # difference is a real bug (no tolerance that could hide one).
                if int(stored_biz) != recalc_biz:
                    add(
                        "BUSINESS_DAYS_MISMATCH",
                        ERROR,
                        f"Stored ACT business days ({stored_biz}) does not match independent recalculation ({recalc_biz}).",
                        "det_period_business_days", str(stored_biz), str(recalc_biz),
                    )

            if (entry_type == "Notification" and stage.startswith("Phase 1")
                    and recalc_biz is not None and recalc_biz < 30):
                add(
                    "PHASE1_PERIOD_TOO_SHORT",
                    WARNING,
                    f"Phase 1 determination period is {recalc_biz} business days; the statutory minimum is 30.",
                    "end_of_determination_period", end_str, "at least 30 business days after notification",
                )

        # ── Holiday table coverage ─────────────────────────────────────────
        for label, d in (("notification_date", notif_date), ("end_of_determination_period", end_date)):
            if d and d.year > HOLIDAY_TABLE_LAST_YEAR:
                add(
                    "HOLIDAY_TABLE_EXPIRED",
                    WARNING,
                    f"{label} ({d}) is after the last year in the ACT holiday table "
                    f"({HOLIDAY_TABLE_LAST_YEAR}). Add the new year's holidays to accc_common.py.",
                    label, str(d), f"<= {HOLIDAY_TABLE_LAST_YEAR}",
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
                "last_modified_accc (article:modified_time) is empty; the scraper may have failed to read meta tag.",
                "last_modified_accc", "", "ISO 8601 timestamp",
            )

    print(f"    Checked {len(register)} entries. Found {len(issues)} issues.")
    return issues


# ---------------------------------------------------------------------------
# Layer 2: Cross-source check (listing pages from this run)
# ---------------------------------------------------------------------------

def parse_listing_for_verification(soup):
    """
    Re-extract the subset of fields that appear on the listing page.
    Uses a completely different approach from scraper.py:
    raw text pattern matching on the card text, no h3 navigation.
    Returns a dict keyed by slug.
    """
    results = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if CASE_PATH_MARKER not in href:
            continue
        if "?" in href or "#" in href:
            continue

        slug = href.rstrip("/").split("/")[-1]
        text = a.get_text(separator="|SEP|", strip=True)

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


def layer2_cross_source(register, session=None):
    """
    Compare listing-page values (from the pages this run's scrape saved)
    against stored data. Catches cases where the detail-page scraper extracted
    a different value than what the listing page shows — a reliable sign of a
    parser error.
    """
    print("\n  Layer 2: Cross-source check (listing pages saved by this run)...")
    issues = []

    pages = cached_listing_pages()
    if not pages and session is not None:
        print("    No cached listing pages; fetching page 1 live.")
        result = session.get(f"{REGISTER_URL}?items_per_page=50&page=0")
        pages = [result.text] if result.ok else []
    if not pages:
        issues.append(issue(
            "REGISTER_INDEX", "Register index page", REGISTER_URL, 2,
            "LISTING_NOT_AVAILABLE", INFO,
            "No listing pages were available to cross-check (scrape did not run in this job).",
        ))
        return issues

    fresh_listing = {}
    for html in pages:
        fresh_listing.update(parse_listing_for_verification(BeautifulSoup(html, "lxml")))

    print(f"    Re-parsed {len(fresh_listing)} entries from {len(pages)} listing page(s).")

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
            if stored_field == "notification_date" and parse_date(fv) and parse_date(fv) == parse_date(sv):
                fv = sv  # same date, different format ("11 Sep" vs "11 September")
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
            "Entry is in stored data but did not appear on any listing page in this run. "
            "It may have been removed from the register, or a listing page failed to load.",
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
    # strip=True drops the whitespace-only text nodes between tags; without it
    # "Label ||| \n ||| Value" never matched the patterns below.
    all_text = soup.get_text(separator=" ||| ", strip=True)

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


def _party_key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _compare_parties(stored_value, li_value):
    """Stored party names that the independent <li> extraction does not contain."""
    from scraper import party_names
    li_keys = [_party_key(x) for x in li_value.split("; ") if x]
    missing = []
    for name in party_names(stored_value):
        key = _party_key(name)
        if key and not any(key in li for li in li_keys):
            missing.append(name)
    return missing


def layer3_rescrape(register, session=None, sample_size=None, live=0):
    """
    Re-parse a sample of the detail pages saved by this run's scrape with an
    independent method and compare field by field against stored data.
    Optionally fetch `live` extra random pages from the site.
    Returns (issues, pages_checked).
    """
    cached = [slug for slug in register if cached_detail_html(slug)]
    if sample_size is None:
        sample_size = max(SAMPLE_MIN, int(len(cached) * SAMPLE_PCT))
    slugs = random.sample(cached, min(sample_size, len(cached)))

    live_slugs = []
    if live and session is not None:
        pool = [s for s in register if s not in slugs]
        live_slugs = random.sample(pool, min(live, len(pool)))

    total = len(slugs) + len(live_slugs)
    print(f"\n  Layer 3: Detail page re-parse ({len(slugs)} saved this run"
          f"{f', {len(live_slugs)} fetched live' if live_slugs else ''})...")

    issues = []
    for i, slug in enumerate(slugs + live_slugs, 1):
        stored = register[slug]
        url = stored.get("url", "")
        t = stored.get("title", slug)

        print(f"    [{i:3d}/{total}] {slug[:65]}")

        if slug in live_slugs:
            try:
                result = session.get(url)
            except AccessBlocked as exc:
                issues.append(issue(slug, t, url, 3, "RESCRAPE_BLOCKED", WARNING, str(exc)))
                break
            html = result.text if result.ok else None
        else:
            html = cached_detail_html(slug)

        if not html:
            issues.append(issue(
                slug, t, url, 3,
                "RESCRAPE_FETCH_FAILED",
                WARNING,
                "Could not fetch this page for the live spot check. "
                "The page may be temporarily unavailable.",
            ))
            continue

        fresh = re_parse_detail_independently(BeautifulSoup(html, "lxml"), url)

        # ── Compare each field ─────────────────────────────────────────────
        for field in FIELDS_TO_COMPARE_IN_RESCRAPE:
            sv = clean(str(stored.get(field, "") or ""))
            fv = clean(str(fresh.get(field, "") or ""))

            # Skip if the independent parser could not extract the field
            # (empty result from re-parser is not necessarily an error)
            if not fv:
                continue
            if field.endswith("date") or field == "end_of_determination_period":
                if parse_date(sv) and parse_date(sv) == parse_date(fv):
                    continue

            if sv != fv:
                issues.append(issue(
                    slug, t, url, 3,
                    "RESCRAPE_VALUE_MISMATCH",
                    ERROR,
                    f"Field '{field}': stored='{sv}', independent re-parse='{fv}'. "
                    "One of the two parsers is wrong, or the page changed since it was scraped.",
                    field, sv, fv,
                ))

        # ── Parties: every stored name should appear in the <li> extraction ─
        for field in ("acquirers", "targets"):
            if fresh.get(field) and stored.get(field):
                missing = _compare_parties(stored[field], fresh[field])
                if missing:
                    issues.append(issue(
                        slug, t, url, 3,
                        "RESCRAPE_PARTY_MISMATCH",
                        WARNING,
                        f"Stored {field} name(s) not found by the independent parser: {'; '.join(missing)[:200]}.",
                        field, stored[field], fresh[field],
                    ))

    print(f"    Re-parse complete. {len([i for i in issues if i['layer'] == 3])} issues found.")
    return issues, total


def layer_documents():
    """Consultation documents that could not be downloaded after repeated attempts."""
    from documents import MAX_DOWNLOAD_ATTEMPTS
    issues = []
    for url, rec in load_json(DOCUMENTS_MANIFEST_JSON, {}).items():
        if rec.get("status") != "downloaded" and rec.get("attempts", 0) >= MAX_DOWNLOAD_ATTEMPTS:
            issues.append(issue(
                rec.get("slug", ""), rec.get("title", ""), rec.get("accc_url", ""), 1,
                "DOCUMENT_DOWNLOAD_GAVE_UP", WARNING,
                f"Consultation document could not be downloaded after {rec['attempts']} attempts "
                f"({rec.get('last_error', '')}). Download it manually: {url}",
                "consultation_docs", rec.get("filename", ""), url,
            ))
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

def run(skip_layer3=False, sample_size=None, seed=None, live=0):
    separator = "=" * 65
    print(f"\n{separator}")
    print("  ACCC Register Verifier")
    print(f"  {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{separator}\n")

    if not os.path.exists(REGISTER_JSON):
        print(f"ERROR: {REGISTER_JSON} not found. Run scraper.py first.")
        sys.exit(1)

    with open(REGISTER_JSON, "r", encoding="utf-8") as f:
        register = json.load(f)

    print(f"  Loaded {len(register)} entries from {REGISTER_JSON}.")

    run_utc = utc_now_iso()

    if seed is not None:
        random.seed(seed)

    all_issues = []

    # Layer 1
    all_issues.extend(layer1_consistency(register))
    all_issues.extend(layer_documents())

    # Layers 2 and 3 use the HTML saved by this run's scrape. A session is
    # only used for an explicit --live spot check or when nothing is cached.
    session = PoliteSession() if live or not cached_listing_pages() else None

    # Layer 2
    try:
        all_issues.extend(layer2_cross_source(register, session))
    except AccessBlocked as exc:
        all_issues.append(issue("REGISTER_INDEX", "Register index page", REGISTER_URL, 2,
                                "LISTING_BLOCKED", WARNING, str(exc)))

    # Layer 3
    actual_sample = 0
    if not skip_layer3:
        layer3_issues, actual_sample = layer3_rescrape(register, session, sample_size, live)
        all_issues.extend(layer3_issues)

    # Save outputs
    print("\n  Saving outputs...")
    save_report(all_issues)
    save_summary(all_issues, len(register), run_utc, actual_sample)

    # Print summary
    errors, warnings, infos = categorise_issues(all_issues)
    print(f"\n{'─' * 65}")
    print("  Verification complete.")
    print(f"  Register entries checked: {len(register)}")
    print(f"  Layer 3 sample size:      {actual_sample}")
    print(f"  Total issues:             {len(all_issues)}")
    print(f"    Errors:   {len(errors)}")
    print(f"    Warnings: {len(warnings)}")
    print(f"    Info:     {len(infos)}")
    print(f"  Result: {'✅ PASSED' if not errors else '❌ FAILED: see data/verification_report.csv'}")
    print(f"{'─' * 65}\n")

    if errors:
        print("  ERRORS:")
        for e in errors[:20]:
            print(f"    [{e['rule']}] {e['title']}: {e['detail']}")
        if len(errors) > 20:
            print(f"    ... and {len(errors) - 20} more errors in the report.")

    # GitHub Actions outputs
    write_github_outputs(
        verification_passed=not errors,
        error_count=len(errors),
        warning_count=len(warnings),
    )

    return not errors  # True = passed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-layer3", action="store_true",
                        help="Skip the detail page re-parse")
    parser.add_argument("--sample", type=int, default=None,
                        help="Override number of saved pages to re-parse in Layer 3")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible Layer 3 sample selection")
    parser.add_argument("--live", type=int, default=0,
                        help="Also fetch N random detail pages live from the ACCC site")
    args = parser.parse_args()

    passed = run(
        skip_layer3=args.skip_layer3,
        sample_size=args.sample,
        seed=args.seed,
        live=args.live,
    )
    sys.exit(0 if passed else 1)
