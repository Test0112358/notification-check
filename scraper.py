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
from workalendar.oceania import AustralianCapitalTerritory

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
ACT_CAL = AustralianCapitalTerritory()

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
]


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
    Return (calendar_days, act_business_days) between two date strings.
    Returns (None, None) if either date cannot be parsed.
    These represent the span of the determination period.
    """
    start = parse_date(notification_date_str)
    end = parse_date(end_date_str)
    if not start or not end or end < start:
        return None, None
    calendar_days = (end - start).days
    try:
        business_days = ACT_CAL.get_working_days_delta(start, end)
    except Exception:
        business_days = None
    return calendar_days, business_days


def clean(text):
    """Normalise whitespace in a string for reliable comparison."""
    if not text:
        return ""
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


def extract_list_after_h3(h3_tag):
    """
    Extract list items associated with an h3 heading.
    Uses find_next() to search the full document tree, not just direct
    siblings — required because Drupal 10 wraps multi-value fields in
    container divs that are not direct siblings of the h3.
    """
    items = []

    # Primary: find the next <ul> anywhere after this h3 in the document.
    # Validate it belongs to this section by confirming no other h3
    # sits between our h3 and the ul.
    ul = h3_tag.find_next("ul")
    if ul:
        if ul.find_previous("h3") is h3_tag:
            for li in ul.find_all("li"):
                text = clean(li.get_text())
                if text:
                    items.append(text)
            if items:
                return items

    # Fallback for non-list structures (<p> per item or <div> containers)
    sib = h3_tag.next_sibling
    while sib is not None:
        if hasattr(sib, "name"):
            if sib.name == "h3":
                break
            elif sib.name == "p":
                text = clean(sib.get_text())
                if text:
                    items.append(text)
            elif sib.name == "div":
                inner = sib.find_all(["li", "dd", "p"])
                if inner:
                    for el in inner:
                        text = clean(el.get_text())
                        if text:
                            items.append(text)
                else:
                    text = clean(sib.get_text())
                    if text:
                        items.append(text)
                break
        sib = sib.next_sibling

    return items
def parse_detail_page(soup, url):
    """
    Parse an individual acquisition/waiver detail page and return a dict of all fields.
    The Drupal CMS renders field labels as h3 tags followed by their values.
    """
    data = {"url": url}

    if not soup:
        return data

    # ── Meta tags ──────────────────────────────────────────────────────────
    # article:modified_time is updated by the ACCC whenever the page is edited.
    # This is our primary change-detection signal.
    meta_mod = soup.find("meta", {"property": "article:modified_time"})
    if meta_mod:
        data["last_modified_accc"] = clean(meta_mod.get("content", ""))

    meta_pub = soup.find("meta", {"property": "article:published_time"})
    if meta_pub:
        data["published_time_accc"] = clean(meta_pub.get("content", ""))

    # ── Page title ─────────────────────────────────────────────────────────
    h1 = soup.find("h1")
    if h1:
        data["title"] = clean(h1.get_text())

    # ── H3-delimited fields ────────────────────────────────────────────────
    # Map the h3 label text (lowercased) to our data field name.
    # Fields with list content (Acquirer, Target, Other) are handled below.
    SCALAR_FIELDS = {
        "acquisition status": "acquisition_status",
        "acquisition case number": "case_number",
        "type": "type",
        "effective notification date": "notification_date",
        "waiver application date": "notification_date",
        "notification / application date": "notification_date",
        "stage": "stage",
        "end of determination period": "end_of_determination_period",
        "accc determination": "accc_determination",
        "determination publication date": "determination_publication_date",
        "anzsic code(s)": "anzsic_codes",
        "anzsic codes": "anzsic_codes",
    }

    LIST_FIELDS = {
        "acquirer(s)": "acquirers",
        "acquirers": "acquirers",
        "target(s) or vendor(s)": "targets",
        "targets or vendors": "targets",
        "target(s)": "targets",
        "other party(ies)": "other_parties",
        "other parties": "other_parties",
    }

    for h3 in soup.find_all("h3"):
        label = clean(h3.get_text()).lower()

        if label in SCALAR_FIELDS:
            val = next_sibling_text(h3)
            if val:
                data[SCALAR_FIELDS[label]] = val

        elif label in LIST_FIELDS:
            items = extract_list_after_h3(h3)
            data[LIST_FIELDS[label]] = "; ".join(items)

    # ── Decisions / documents table ────────────────────────────────────────
    docs = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            date_text = clean(cells[0].get_text())
            desc_text = clean(cells[1].get_text())
            link_tag = cells[-1].find("a") if len(cells) > 2 else cells[1].find("a")
            doc_url = urljoin(BASE_URL, link_tag["href"]) if link_tag else ""
            if date_text and desc_text:
                docs.append({
                    "date": date_text,
                    "description": desc_text,
                    "url": doc_url,
                })
    if docs:
        data["documents"] = docs

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
            "Notification / Application Date":          e.get("notification_date", ""),
            "Stage":                                    e.get("stage", ""),
            "Acquisition Status":                       e.get("acquisition_status", ""),
            "End of Determination Period":              e.get("end_of_determination_period", ""),
            # Calculated span: notification date → end of determination period
            "Determination Period Calendar Days (ACT)": e.get("det_period_calendar_days", ""),
            "Determination Period Business Days (ACT)": e.get("det_period_business_days", ""),
            # For completed cases: notification date → determination publication date
            "Elapsed Calendar Days to Decision":        e.get("elapsed_calendar_days", ""),
            "Elapsed Business Days to Decision (ACT)":  e.get("elapsed_business_days", ""),
            "ACCC Determination":                       e.get("accc_determination", ""),
            "Determination Publication Date":           e.get("determination_publication_date", ""),
            "ANZSIC Code(s)":                           e.get("anzsic_codes", ""),
            "URL":                                      e.get("url", ""),
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

    return has_changes


if __name__ == "__main__":
    run()
