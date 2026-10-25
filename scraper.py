#!/usr/bin/env python3
"""
ACCC Acquisitions Register Monitor
====================================
Scrapes the ACCC acquisitions register, detects field-level changes (including
date changes on individual case pages), and exports the dataset as JSON, CSV,
Excel and a structured changelog.

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

Request budget
--------------
Every run fetches the listing pages (~15 requests). Detail pages are then
fetched only where something may have changed:
  - new entries, and entries whose listing values (status, stage, etc.) changed
  - every matter still under assessment
  - matters decided or modified in the last RECENT_DAYS days (the ACCC often
    adds documents after a determination)
  - entries whose previous fetch failed
  - a rolling sweep of the least-recently-checked remaining pages, sized so
    every page is still re-checked about once a week
Set FULL_REFRESH=1 to fetch every detail page.
"""

import math
import os
import re
import sys
import traceback
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import documents
import reporting
from accc_common import (
    BASE_URL, CASE_PATH_MARKER, CHANGELOG_JSON, HEALTH_JSON, LATEST_CHANGES_JSON,
    LISTING_PAGE_SIZE, NEW_DOCUMENTS_JSON, REGISTER_JSON, REGISTER_URL,
    SCHEMA_VERSION, AccessBlocked, PoliteSession, cache_detail_html,
    cache_listing_html, calc_period_days, clean, load_health,
    load_json, parse_date, parse_iso, save_json, utc_now, utc_now_iso,
    write_github_outputs,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RECENT_DAYS = 21          # keep re-checking matters decided/modified this recently
SWEEP_RUNS_PER_CYCLE = 10 # 2 runs a weekday → every page re-checked about weekly

# Sanity guard: abort without saving if the site or parser looks broken
MIN_LISTING_RATIO = 0.8   # listing must return at least 80% of stored entries
MAX_BLANKED_RATIO = 0.25  # no more than 25% of fetched pages may lose a key field
SANITY_FIELDS = ["title", "case_number", "notification_date", "acquisition_status", "acquirers"]

# Fields copied from the listing card (also used to decide whether to re-fetch)
LISTING_FIELDS = ["acquisition_status", "type", "case_number", "stage", "notification_date"]

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
    "consultation_close_date",
    "consultation_docs",
    "decisions_docs",
]

# Changes to these fields never trigger alerts on their own; they are still
# recorded in the changelog with "material": false.
COSMETIC_FIELDS = {"last_modified_accc", "consultation_text"}

# On the first run after SCHEMA_VERSION is bumped, differences in these fields
# come from the parser upgrade (identifier pairing, de-duplicated text), not
# from the ACCC, so they are recorded as cosmetic.
SCHEMA_REFORMATTED_FIELDS = {"acquirers", "targets", "other_parties", "consultation_text"}


class SanityCheckFailed(Exception):
    """The scraped data looks wrong; refuse to overwrite good stored data."""


# ---------------------------------------------------------------------------
# Listing page parsing
# ---------------------------------------------------------------------------

PAGE_PARAM = re.compile(r"[?&]page=(\d+)")


def get_last_page_index(soup):
    """Extract the 0-based index of the last page from pagination links."""
    # Drupal pager: "Go to last page" link
    last_link = soup.find("a", title=re.compile(r"Go to last page", re.I))
    if last_link:
        # [?&] anchor: a bare "page=" would also match inside "items_per_page=50"
        m = PAGE_PARAM.search(last_link.get("href", ""))
        if m:
            return int(m.group(1))
    # Fallback: find the highest page= value among all pager links
    max_page = 0
    for a in soup.select("li.pager__item a, .pager a"):
        m = PAGE_PARAM.search(a.get("href", ""))
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


# The first line is the case title (before any field labels)
CARD_LABEL_MAP = {
    "acquisition status": "acquisition_status",
    "type": "type",
    "case number": "case_number",
    "stage": "stage",
    "effective notification date": "notification_date",
    "waiver application date": "notification_date",
    "notification / application date": "notification_date",
}


def parse_card(a_tag):
    """
    Parse a single acquisition card <a> element from the listing page.
    The card contains the title + structured key/value labels as child elements.
    Returns a dict with summary-level fields, or None if the link is not a case page.
    """
    href = a_tag.get("href", "")

    # Must be an individual case page (not the register index or a filter link).
    # Match on the stable register segment so a change to the parent path
    # ordering (as happened Aug 2026) does not silently drop every card.
    if CASE_PATH_MARKER not in href:
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

    title = ""
    for i, line in enumerate(lines):
        if line.lower() in CARD_LABEL_MAP or any(line.lower().startswith(k) for k in CARD_LABEL_MAP):
            title = " ".join(lines[:i]).strip()
            break
    if not title and lines:
        title = lines[0]

    entry = {
        "slug": slug,
        "url": full_url,
        "title": clean(title),
    }

    i = 0
    while i < len(lines):
        field = CARD_LABEL_MAP.get(lines[i].lower())
        if field and i + 1 < len(lines):
            entry[field] = clean(lines[i + 1])
            i += 1
        i += 1

    return entry


def parse_listing_page(soup, entries):
    for a in soup.find_all("a", href=True):
        card = parse_card(a)
        if card and card["slug"] not in entries:
            entries[card["slug"]] = card


def fetch_all_listing_entries(session):
    """
    Paginate through all listing pages and return a dict of {slug: entry}.
    Uses 50 items per page to minimise total requests.
    Raises AccessBlocked if the first page is refused.
    """
    print("  Fetching register listing pages...")
    entries = {}

    first_url = f"{REGISTER_URL}?items_per_page={LISTING_PAGE_SIZE}&page=0"
    result = session.get(first_url)
    if result.status == 403:
        raise AccessBlocked(
            "The ACCC website returned 403 Forbidden for the register listing page. "
            "The site's CDN is refusing requests from this machine."
        )
    if not result.ok:
        raise RuntimeError(f"Could not fetch the register listing page ({result.error or result.status}).")

    cache_listing_html(0, result.text)
    soup = BeautifulSoup(result.text, "lxml")
    total_pages = get_last_page_index(soup) + 1
    print(f"  {total_pages} page(s) found at {LISTING_PAGE_SIZE} items/page.")
    parse_listing_page(soup, entries)

    failed_pages = []
    for page in range(1, total_pages):
        url = f"{REGISTER_URL}?items_per_page={LISTING_PAGE_SIZE}&page={page}"
        print(f"    Page {page + 1}/{total_pages}...")
        result = session.get(url)
        if result.ok:
            cache_listing_html(page, result.text)
            parse_listing_page(BeautifulSoup(result.text, "lxml"), entries)
        else:
            failed_pages.append(page + 1)

    if failed_pages:
        print(f"  WARNING: listing page(s) {failed_pages} could not be fetched.")
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


# Page furniture that can appear between party headings and must not be
# mistaken for a company name.
_UI_WORDS = {
    "share", "print", "email", "top", "more", "less", "menu", "home", "next",
    "previous", "back", "show more", "show less", "read more", "read less",
    "back to top", "expand", "collapse", "close",
}

_IDENTIFIER_KEYWORDS = re.compile(
    r"\b(ABN|ACN|ARBN|ARSN|CRN|NZBN|NZCN|UEN|EIN|LEI|KVK|SIREN|SIRET|HRB|HRA|CCI|"
    r"SEC|IRS|CRD|JFSC|CBN|BRN|CNPJ|CIN|UIC|FN|RCS|R\.C\.S|"
    r"tax\s*id|employer\s+identification(\s+number)?|"
    r"(company|corporate|corporation|registration|registered|file|business|"
    r"incorporation|entity|state)\s*(registration\s*)?(number|no\.?|id|code)|"
    r"(CR|reg)\.?\s*(no\.?|number)?|reg\.|no\.|EUID|UKCRN|identif\w*(\s+number)?|"
    r"registered\s+(in|at|with)|register|registre|chamber\s+of\s+commerce|"
    r"charter\s+number|department(\s+id)?|division\s+of\s+corporations|"
    r"LP\s+number|financial\s+centre)(?=\W|\d|$)",
    re.IGNORECASE,
)
_COMPANY_WORDS = re.compile(
    r"\b(pty|ltd|limited|inc|llc|lp|l\.p\.|gmbh|b\.v\.|bv|s\.a\.|sa|plc|ag|nv|"
    r"trust|trustee|fund|holdings?|group|corporation|corp|company|co\.)\b",
    re.IGNORECASE,
)


def is_identifier_line(line):
    """
    True if a line is a company/registration number rather than a party name,
    e.g. "695 902 955", "ABN - 11 164 311 997", "US Tax ID: 81-0586437",
    "Netherlands registration number (KVK): 34249320".
    """
    digits = re.sub(r"\D", "", line)
    if len(digits) < 5:
        return False
    if re.fullmatch(r"[\d\s\-./:#]+", line):
        return True
    first_digit = re.search(r"\d", line).start()
    before, after = line[:first_digit], line[first_digit:]
    # "Blackbird FOF25 Pty Ltd as trustee for …" is a name that contains digits
    if _COMPANY_WORDS.search(after):
        return False
    has_keyword = bool(_IDENTIFIER_KEYWORDS.search(before))
    # "Company number (Delaware): 123" is an identifier even though it says "Company"
    remainder = _IDENTIFIER_KEYWORDS.sub(" ", before)
    if _COMPANY_WORDS.search(remainder) or len(remainder.split()) > 6:
        return False
    # "Spain A64622970", "B167972": a short prefix glued to a long number
    short_prefix = (len(remainder.split()) <= 2 and len(digits) >= 6
                    and len(re.findall(r"[A-Za-z]", after)) <= 3)
    # "7016672 - Delaware", "105254478 – Ontario, Canada"
    number_first = not before.strip() and len(after.split()) <= 5
    return has_keyword or before.rstrip().endswith((":", "-", "#")) or short_prefix or number_first


def format_identifier(line):
    """Label bare Australian numbers: 9 digits → ACN, 11 digits → ABN."""
    line = clean(line).strip(" ;,")
    if re.fullmatch(r"[\d\s]+", line):
        digits = re.sub(r"\D", "", line)
        if len(digits) == 9:
            return f"ACN {line}"
        if len(digits) == 11:
            return f"ABN {line}"
    return line


def pair_party_lines(lines):
    """
    Turn raw party lines into one item per party, attaching identifier lines
    to the party before them:
      ["Smile Partners (NSW3) Pty Ltd", "695 902 955"]
        → ["Smile Partners (NSW3) Pty Ltd (ACN 695 902 955)"]
    """
    parties = []
    for raw in lines:
        line = clean(raw)
        if not line or not re.search(r"[A-Za-z0-9]", line):
            continue
        if line.lower() in _UI_WORDS:
            continue
        if is_identifier_line(line) and parties:
            ident = format_identifier(line)
            name = parties[-1]
            if name.endswith(")") and "(" in name and _IDENTIFIER_KEYWORDS.search(name[name.rfind("("):]):
                parties[-1] = name[:-1] + f"; {ident})"
            else:
                parties[-1] = f"{name} ({ident})"
        else:
            parties.append(line)
    return parties


def party_names(value):
    """Party names only (identifiers removed), for display and watchlist matching."""
    names = []
    for item in split_parties(value):
        m = re.search(r"\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$", item)
        if m and len(re.sub(r"\D", "", m.group(1))) >= 5:
            item = item[:m.start()]
        if item.strip():
            names.append(item.strip())
    return names


def split_parties(value):
    """Split a stored party field into items without breaking identifiers like '(ACN …; CRN …)'."""
    if not value:
        return []
    items, depth, current = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            if current.strip():
                items.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current.strip())
    return items


def extract_parties_from_text(page_text, section_label, stop_labels):
    """
    Extract party lines from raw page text between a section header and the next.
    Bypasses DOM structure entirely — works regardless of Drupal's HTML nesting.
    """
    lines = page_text.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        if clean(line).lower() == section_label.lower():
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    stop_set = {s.lower() for s in stop_labels}
    items = []
    for line in lines[start_idx:]:
        stripped = clean(line)
        if stripped.lower() in stop_set:
            break
        if stripped:
            items.append(stripped)

    return pair_party_lines(items)


def _strip_read_more(text):
    text = re.sub(r"\s*(read more|read less|show more|show less)\s*$", "", text, flags=re.I)
    return text.rstrip(" .…")


def dedupe_text_parts(parts):
    """
    Drop teaser/summary blocks that repeat the start of a fuller block.
    The consultation section shows a short summary and the full text, which
    previously produced "The ACCC has prepared… The ACCC has prepared…".
    """
    stripped = [_strip_read_more(p) for p in parts]
    keep = []
    for i, p in enumerate(stripped):
        if not p:
            continue
        repeated = any(
            j != i and len(q) > len(p) and q.startswith(p)
            for j, q in enumerate(stripped)
        ) or p in [stripped[k] for k in keep]
        if not repeated:
            keep.append(i)
    return [parts[i] if not re.search(r"read (more|less)$", parts[i], re.I) else stripped[i]
            for i in keep]


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

ALL_STOP_LABELS = [
    "Acquirer(s)", "Target(s) or Vendor(s)", "Other party(ies)",
    "ANZSIC code(s)", "ANZSIC codes", "Description", "Consultation",
    "Decisions and key events", "About the acquisition",
    "ACCC Determination", "Determination publication date",
    "Acquisition status", "Acquisition case number",
]

DECISION_SECTION_LABELS = (
    "decisions and key events",
    "key events",
    "decisions",
    "decisions & key events",
)


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
    for h3 in soup.find_all("h3"):
        label = clean(h3.get_text()).lower()
        if label in SCALAR_FIELDS:
            val = next_sibling_text(h3)
            if val:
                data[SCALAR_FIELDS[label]] = val

    # Party fields via text extraction
    page_text = soup.get_text(separator="\n")
    for label, field in (("Acquirer(s)", "acquirers"),
                         ("Target(s) or Vendor(s)", "targets"),
                         ("Other party(ies)", "other_parties")):
        parties = extract_parties_from_text(
            page_text, label, [s for s in ALL_STOP_LABELS if s != label]
        )
        if parties:
            data[field] = "; ".join(parties)

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
            text_parts = dedupe_text_parts(text_parts)
            if text_parts:
                data["consultation_text"] = " ".join(text_parts[:3])
                close = documents.parse_consultation_close_date(data["consultation_text"])
                if close:
                    data["consultation_close_date"] = close

            # Consultation document table (Word docs or PDFs)
            rows = parse_table_rows(get_section_table(h3, all_h3_and_tables))
            if rows:
                data["consultation_docs"] = rows

        elif label in DECISION_SECTION_LABELS:
            # Decisions document table (PDFs or Word docs)
            rows = parse_table_rows(get_section_table(h3, all_h3_and_tables))
            if rows:
                data["decisions_docs"] = rows

    # Combined documents field for backward compatibility
    all_docs = data.get("consultation_docs", []) + data.get("decisions_docs", [])
    if all_docs:
        data["documents"] = all_docs

    return data


# ---------------------------------------------------------------------------
# Which detail pages to fetch this run
# ---------------------------------------------------------------------------

def _recently_active(entry, now):
    decided = parse_date(entry.get("determination_publication_date", ""))
    if decided and (now.date() - decided).days <= RECENT_DAYS:
        return True
    mod = parse_iso(entry.get("last_modified_accc", ""))
    return bool(mod and (now - mod).days <= RECENT_DAYS)


def listing_value_differs(field, card_val, stored_val):
    """Compare a listing card value with the stored (detail page) value, tolerant of formatting."""
    if not card_val:
        return False
    if field == "notification_date":
        a, b = parse_date(card_val), parse_date(stored_val)
        if a and b:
            return a != b
    return clean(card_val).lower() != clean(stored_val).lower()


def select_detail_fetches(listing_entries, stored_register, full_refresh=False, now=None):
    """
    Return ({slug: reason}) for detail pages to fetch this run. Everything not
    selected keeps its stored detail data (still updated from the listing).
    """
    now = now or utc_now()
    if full_refresh:
        return {slug: "full refresh" for slug in listing_entries}

    selected = {}
    sweep_pool = []
    for slug, card in listing_entries.items():
        stored = stored_register.get(slug)
        if not stored:
            selected[slug] = "new entry"
        elif any(listing_value_differs(f, card.get(f, ""), stored.get(f, "")) for f in LISTING_FIELDS):
            selected[slug] = "listing changed"
        elif stored.get("acquisition_status") == "Under assessment":
            selected[slug] = "under assessment"
        elif stored.get("detail_fetch_failed_utc"):
            selected[slug] = "retry failed fetch"
        elif _recently_active(stored, now):
            selected[slug] = "recently decided/modified"
        else:
            sweep_pool.append(slug)

    # Rolling sweep: least recently checked first
    batch = math.ceil(len(listing_entries) / SWEEP_RUNS_PER_CYCLE) if listing_entries else 0
    sweep_pool.sort(key=lambda s: stored_register[s].get("last_checked_utc")
                    or stored_register[s].get("last_scraped_utc", ""))
    for slug in sweep_pool[:batch]:
        selected[slug] = "weekly sweep"
    return selected


# ---------------------------------------------------------------------------
# Derived fields and change detection
# ---------------------------------------------------------------------------

def add_derived_fields(entry):
    """Calendar/business day spans (Day 0 = notification date)."""
    notif_date = entry.get("notification_date", "")
    cal, biz = calc_period_days(notif_date, entry.get("end_of_determination_period", ""))
    entry["det_period_calendar_days"] = cal
    entry["det_period_business_days"] = biz
    det_pub = entry.get("determination_publication_date", "")
    if det_pub:
        e_cal, e_biz = calc_period_days(notif_date, det_pub)
        entry["elapsed_calendar_days"] = e_cal
        entry["elapsed_business_days"] = e_biz
    else:
        entry.pop("elapsed_calendar_days", None)
        entry.pop("elapsed_business_days", None)
    return entry


def migrate_stored_entry(entry):
    """Bring a record stored by an older parser up to SCHEMA_VERSION in memory."""
    if entry.get("schema_version", 1) >= SCHEMA_VERSION:
        return entry
    entry = dict(entry)
    for field in ("acquirers", "targets", "other_parties"):
        if entry.get(field):
            entry[field] = "; ".join(pair_party_lines(split_parties(entry[field])))
    if entry.get("consultation_text"):
        close = documents.parse_consultation_close_date(entry["consultation_text"])
        if close:
            entry["consultation_close_date"] = close
    if "last_checked_utc" not in entry and entry.get("last_scraped_utc"):
        entry["last_checked_utc"] = entry["last_scraped_utc"]
    add_derived_fields(entry)
    return entry


def _docs_material(old_docs, new_docs):
    """
    A document change is material when a row is added. A URL being filled in,
    or the consultation table disappearing after consultation closes, is not.
    """
    return bool(documents.new_documents(old_docs, new_docs))


def detect_changes(old, new, schema_upgrade=False):
    """
    Compare two entry dicts across TRACKED_FIELDS.
    Returns a list of {"field", "old", "new", "material"} dicts for changed fields.
    """
    changes = []
    for field in TRACKED_FIELDS:
        old_val = clean(str(old.get(field, "") or ""))
        new_val = clean(str(new.get(field, "") or ""))
        if old_val == new_val:
            continue
        if field in ("consultation_docs", "decisions_docs"):
            material = _docs_material(old.get(field) or [], new.get(field) or [])
        elif field == "consultation_close_date" and not new_val:
            material = False  # section removed once consultation closes
        else:
            material = field not in COSMETIC_FIELDS
        if schema_upgrade and field in SCHEMA_REFORMATTED_FIELDS:
            material = False
        changes.append({"field": field, "old": old_val, "new": new_val, "material": material})
    return changes


def check_sanity(listing_entries, stored_register, fetched):
    """Raise SanityCheckFailed if the run would overwrite good data with blanks."""
    if len(stored_register) >= 20 and len(listing_entries) < MIN_LISTING_RATIO * len(stored_register):
        raise SanityCheckFailed(
            f"The listing returned only {len(listing_entries)} entries but {len(stored_register)} "
            "are stored. The register page may have changed or been partially served."
        )
    for field in SANITY_FIELDS:
        had = [slug for slug, entry in fetched.items() if stored_register.get(slug, {}).get(field)]
        lost = [slug for slug in had if not fetched[slug].get(field)]
        if len(had) >= 10 and len(lost) / len(had) > MAX_BLANKED_RATIO:
            raise SanityCheckFailed(
                f"'{field}' came back empty for {len(lost)} of {len(had)} fetched pages that "
                "previously had it. The ACCC page layout has probably changed; the parser "
                "in parse_detail_page() needs updating. Stored data was left untouched."
            )


# ---------------------------------------------------------------------------
# Removed entries
# ---------------------------------------------------------------------------

def check_potentially_removed(potentially_removed, stored_register, new_register, session, run_utc):
    """
    Entries missing from the listing are confirmed individually. Returns
    (removed_slugs, recovered {slug: detail}) — recovered entries go through the
    normal change-detection pipeline.
    """
    removed_slugs, recovered = [], {}
    if potentially_removed:
        print(f"\n  Verifying {len(potentially_removed)} potentially removed entry(ies)...")

    # Case numbers already present under a fresh slug = renamed deal
    fresh_cases = {e.get("case_number", "") for e in new_register.values() if e.get("case_number")}

    for slug in potentially_removed:
        stored_entry = stored_register[slug]

        if stored_entry.get("case_number") and stored_entry["case_number"] in fresh_cases:
            removed_slugs.append(slug)
            print(f"    Renamed duplicate, dropping stale slug: {slug}")
            continue

        detail_url = stored_entry.get("url") or f"{REGISTER_URL}/{slug}"
        result = session.get(detail_url)
        if result.status in (404, 410):
            removed_slugs.append(slug)
            print(f"    CONFIRMED removed: {slug}")
        elif result.ok:
            cache_detail_html(slug, result.text)
            recovered[slug] = parse_detail_page(BeautifulSoup(result.text, "lxml"), detail_url)
            print(f"    Recovered (pagination miss, re-parsed live): {slug}")
        else:
            # Unknown state (403, 5xx, network): keep the stored record as-is
            print(f"    Cannot verify {slug} ({result.error or result.status}); keeping stored record")
    return removed_slugs, recovered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _changelog_event(run_utc, slug, entry, event, changes=None):
    changes = changes or []
    return {
        "timestamp_utc": run_utc,
        "slug": slug,
        "title": entry.get("title", slug),
        "case_number": entry.get("case_number", ""),
        "url": entry.get("url", ""),
        "event": event,
        "material": event != "CHANGED" or any(c.get("material", True) for c in changes),
        "changes": changes,
    }


def scrape(session, run_utc, full_refresh=False):
    stored_raw = load_json(REGISTER_JSON, {})
    schema_upgrade = any(e.get("schema_version", 1) < SCHEMA_VERSION for e in stored_raw.values())
    stored_register = {slug: migrate_stored_entry(e) for slug, e in stored_raw.items()}
    changelog = load_json(CHANGELOG_JSON, [])
    first_event = len(changelog)

    # ── Step 1: Get summary data from all listing pages ────────────────────
    listing_entries = fetch_all_listing_entries(session)

    # ── Step 2: Fetch the detail pages that may have changed ───────────────
    to_fetch = select_detail_fetches(listing_entries, stored_register, full_refresh)
    reasons = {}
    for reason in to_fetch.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"\nFetching {len(to_fetch)} of {len(listing_entries)} detail pages "
          f"({', '.join(f'{n} {r}' for r, n in sorted(reasons.items()))})...\n")

    fetched = {}
    for idx, slug in enumerate(to_fetch, 1):
        card = listing_entries[slug]
        print(f"  [{idx:3d}/{len(to_fetch)}] {slug[:70]}")
        stored = stored_register.get(slug, {})
        result = session.get(card["url"], etag=stored.get("http_etag", ""),
                             last_modified=stored.get("http_last_modified", ""))
        if result.not_modified and stored:
            fetched[slug] = {"_not_modified": True}
        elif result.ok:
            cache_detail_html(slug, result.text)
            detail = parse_detail_page(BeautifulSoup(result.text, "lxml"), card["url"])
            detail["http_etag"] = result.headers.get("ETag", "")
            detail["http_last_modified"] = result.headers.get("Last-Modified", "")
            fetched[slug] = detail
        else:
            fetched[slug] = None
            print(f"           WARNING: fetch failed ({result.error or result.status}); keeping stored data")

    check_sanity(listing_entries, stored_register,
                 {s: d for s, d in fetched.items() if d and not d.get("_not_modified")})

    # ── Step 3: Merge, derive, detect changes ──────────────────────────────
    new_register, new_slugs, changed_slugs = {}, [], []

    def process(slug, card, detail):
        stored = stored_register.get(slug, {})
        # Only listing values that the detail page does not refine are applied
        # on top of stored records (the card title can differ from the h1).
        card_fields = {k: card[k] for k in LISTING_FIELDS
                       if listing_value_differs(k, card.get(k, ""), stored.get(k, ""))}
        if card.get("url"):
            card_fields["url"] = card["url"]
        if detail is None:                       # fetch failed: keep stored detail data
            entry = {**stored, **card_fields} if stored else dict(card)
            entry["detail_fetch_failed_utc"] = run_utc
        elif detail is False:                    # not selected this run
            entry = {**stored, **card_fields}
        elif detail.get("_not_modified"):        # 304: page unchanged
            entry = {**stored, **card_fields, "last_checked_utc": run_utc}
            entry.pop("detail_fetch_failed_utc", None)
        else:
            # Merge: detail page values override list-page values for shared fields.
            # Fields that disappear from the page are dropped (not carried over),
            # except the deadline below.
            entry = {**card, **detail, "last_checked_utc": run_utc}
        if stored.get("first_seen_utc"):
            entry["first_seen_utc"] = stored["first_seen_utc"]
        entry["slug"] = slug
        entry["last_seen_in_listing_utc"] = run_utc
        entry.setdefault("first_seen_utc", stored.get("first_seen_utc") or run_utc)
        entry["schema_version"] = SCHEMA_VERSION
        entry.pop("last_scraped_utc", None)

        # Preserve the deadline after the ACCC blanks it on determination.
        # The date remains legally meaningful for elapsed-time analysis.
        if not entry.get("end_of_determination_period") and stored.get("end_of_determination_period"):
            entry["end_of_determination_period"] = stored["end_of_determination_period"]
        add_derived_fields(entry)

        if not stored:
            new_slugs.append(slug)
            changelog.append(_changelog_event(run_utc, slug, entry, "NEW_ENTRY"))
        else:
            changes = detect_changes(stored, entry, schema_upgrade)
            if changes:
                changelog.append(_changelog_event(run_utc, slug, entry, "CHANGED", changes))
                if any(c["material"] for c in changes):
                    changed_slugs.append(slug)
        new_register[slug] = entry

    for slug, card in listing_entries.items():
        process(slug, card, fetched.get(slug, False))

    # ── Removed entries ────────────────────────────────────────────────────
    potentially_removed = sorted(set(stored_register) - set(new_register))
    removed_slugs, recovered = check_potentially_removed(
        potentially_removed, stored_register, new_register, session, run_utc)
    for slug, detail in recovered.items():
        process(slug, {"slug": slug, "url": detail.get("url", "")}, detail)
    for slug in potentially_removed:
        if slug not in removed_slugs and slug not in new_register:
            new_register[slug] = stored_register[slug]
    for slug in removed_slugs:
        changelog.append(_changelog_event(run_utc, slug, stored_register[slug], "REMOVED"))

    return {
        "stored_register": stored_register,
        "new_register": new_register,
        "changelog": changelog,
        "this_run_events": changelog[first_event:],
        "new_slugs": new_slugs,
        "changed_slugs": changed_slugs,
        "removed_slugs": removed_slugs,
        "fetched_count": len(to_fetch),
        "listing_count": len(listing_entries),
        "schema_upgrade": schema_upgrade,
    }


def update_documents(new_register, session, run_utc):
    """Track and download consultation documents. Never loses a document on failure."""
    manifest = documents.load_manifest()
    added = documents.sync_manifest(manifest, new_register, run_utc)
    if added:
        print(f"  {added} consultation document(s) newly tracked.")
    downloaded = []
    try:
        downloaded = documents.download_pending(manifest, session, run_utc)
    except AccessBlocked as exc:
        print(f"  WARNING: downloads stopped early: {exc}")
    finally:
        documents.save_manifest(manifest)
    counts = documents.manifest_counts(manifest)
    print(f"  Documents: {counts['downloaded']} downloaded, {counts['pending']} pending, "
          f"{counts['failed']} failed (will retry), {counts['gave_up']} gave up.")
    return manifest, downloaded


def record_health(health, run_utc, status, error="", request_count=0):
    health["last_attempt_utc"] = run_utc
    health["last_status"] = status
    health["last_request_count"] = request_count
    if status == "success":
        health["last_success_utc"] = run_utc
        health["consecutive_failures"] = 0
        health["last_error"] = ""
    else:
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["last_error"] = error
    save_json(HEALTH_JSON, health)
    return health


def _seed_health(health):
    """First run with health.json: take the last success from existing data."""
    if not health.get("last_success_utc"):
        latest = load_json(LATEST_CHANGES_JSON, {})
        if latest.get("run_utc"):
            health["last_success_utc"] = latest["run_utc"]
    return health


def should_alert_failure(consecutive_failures):
    """Alert on the first failure, then about once a day (every 2nd scheduled run)."""
    return consecutive_failures == 1 or consecutive_failures % 2 == 0


def rebuild():
    """
    Regenerate register.json derived fields, CSV, Excel and summary from stored
    data without contacting the ACCC site (e.g. after a calculation fix, or
    after editing watchlist.json). Records keep their schema_version so the
    next live run still treats format differences as cosmetic.
    """
    stored = load_json(REGISTER_JSON, {})
    register = {slug: migrate_stored_entry(e) for slug, e in stored.items()}
    for slug, entry in register.items():
        entry["schema_version"] = stored[slug].get("schema_version", 1)
        add_derived_fields(entry)
    save_json(REGISTER_JSON, register)

    manifest = documents.load_manifest()
    added = documents.sync_manifest(manifest, register, utc_now_iso())
    documents.save_manifest(manifest)
    print(f"  {added} consultation document(s) tracked; downloads happen on the next live run.")

    health = _seed_health(load_health())
    watchlist = reporting.load_watchlist()
    rows = reporting.register_rows(register, manifest, watchlist)
    reporting.export_csv(rows)
    summary = reporting.generate_summary(register, health, manifest)
    save_json(reporting.SUMMARY_JSON, summary)
    changelog = load_json(CHANGELOG_JSON, [])
    reporting.export_excel(register, rows, changelog, summary["analytics"], health,
                           health.get("last_success_utc") or utc_now_iso(),
                           {"new": 0, "changed": 0, "removed": 0, "decisions": 0, "documents": 0})
    print(f"  Rebuilt outputs for {len(register)} entries.")
    return 0


def run(http_session=None):
    """Run one monitoring pass. http_session lets tests supply a fake requests.Session."""
    separator = "=" * 65
    print(f"\n{separator}")
    print("  ACCC Acquisitions Register Monitor")
    print(f"  {utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{separator}\n")

    run_utc = utc_now_iso()
    full_refresh = os.environ.get("FULL_REFRESH", "").lower() in ("1", "true", "yes")
    health = _seed_health(load_health())
    session = PoliteSession(http_session)

    try:
        result = scrape(session, run_utc, full_refresh)
    except Exception as exc:
        kind = ("blocked" if isinstance(exc, AccessBlocked)
                else "sanity" if isinstance(exc, SanityCheckFailed) else "error")
        message = str(exc) if kind != "error" else f"{type(exc).__name__}: {exc}"
        if kind == "error":
            traceback.print_exc()
        print(f"\nRUN FAILED ({kind}): {message}")
        print("Stored data has NOT been modified.")
        health = record_health(health, run_utc, f"failed: {kind}", message, session.request_count)
        reporting.write_failure_email(health, kind, message)
        write_github_outputs(
            scrape_ok=False,
            has_changes=False,
            failure_kind=kind,
            consecutive_failures=health["consecutive_failures"],
            alert_failure=should_alert_failure(health["consecutive_failures"]),
            failure_subject=reporting.failure_subject(health, kind),
            run_utc=run_utc,
        )
        return 2 if kind == "blocked" else 3 if kind == "sanity" else 1

    new_register = result["new_register"]
    stored_register = result["stored_register"]
    changelog = result["changelog"]
    new_slugs, changed_slugs, removed_slugs = (
        result["new_slugs"], result["changed_slugs"], result["removed_slugs"])

    # ── Persist ────────────────────────────────────────────────────────────
    print("\nSaving data files...")
    save_json(REGISTER_JSON, new_register)
    save_json(CHANGELOG_JSON, changelog)

    # ── Documents ──────────────────────────────────────────────────────────
    print("\nChecking consultation documents...")
    manifest, downloaded = update_documents(new_register, session, run_utc)
    this_run_events = result["this_run_events"]
    decision_alerts = reporting.collect_decision_alerts(this_run_events, stored_register, new_register)
    save_json(NEW_DOCUMENTS_JSON, {
        "run_utc": run_utc,
        "questionnaire_count": len(downloaded),   # kept for backward compatibility
        "consultation_document_count": len(downloaded),
        "questionnaires": downloaded,
        "decision_documents": decision_alerts,
    })

    health = record_health(health, run_utc, "success", request_count=session.request_count)

    # ── Reports and emails ─────────────────────────────────────────────────
    outputs = reporting.build_all_outputs(
        run_utc=run_utc,
        register=new_register,
        stored_register=stored_register,
        changelog=changelog,
        this_run_events=this_run_events,
        new_slugs=new_slugs,
        changed_slugs=changed_slugs,
        removed_slugs=removed_slugs,
        downloaded_docs=downloaded,
        manifest=manifest,
        decision_alerts=decision_alerts,
        health=health,
        stats={
            "requests": session.request_count,
            "fetched": result["fetched_count"],
            "listing": result["listing_count"],
            "schema_upgrade": result["schema_upgrade"],
        },
    )
    save_json(HEALTH_JSON, health)  # digest week may have been recorded

    # ── Summary output ─────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"  Run complete at {run_utc}")
    print(f"  Total entries:     {len(new_register)}")
    print(f"  Detail pages:      {result['fetched_count']} fetched")
    print(f"  HTTP requests:     {session.request_count}")
    print(f"  New entries:       {len(new_slugs)}")
    print(f"  Changed (material):{len(changed_slugs):>3}")
    print(f"  Removed entries:   {len(removed_slugs)}")
    print(f"  Documents saved:   {len(downloaded)}")
    print(f"{'─' * 65}")

    for s in new_slugs:
        print(f"    + {new_register[s].get('title', s)}")
    for event in this_run_events:
        if event["event"] == "CHANGED" and event["material"]:
            print(f"    ~ {event['title']}")
            for ch in event["changes"]:
                if ch["material"]:
                    print(f"        {reporting.describe_change(ch)}")
    for s in removed_slugs:
        print(f"    - {stored_register[s].get('title', s)}")

    has_changes = bool(new_slugs or changed_slugs or removed_slugs)
    write_github_outputs(
        scrape_ok=True,
        has_changes=has_changes,
        new_count=len(new_slugs),
        changed_count=len(changed_slugs),
        removed_count=len(removed_slugs),
        consultation_doc_count=outputs["attached_doc_count"],
        documents_email=outputs["documents_email"],
        documents_subject=outputs["documents_subject"],
        changes_subject=outputs["changes_subject"],
        watchlist_email=outputs["watchlist_email"],
        watchlist_subject=outputs["watchlist_subject"],
        digest_email=outputs["digest_email"],
        digest_subject=outputs["digest_subject"],
        run_utc=run_utc,
        run_local=outputs["run_local"],
        consecutive_failures=0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(rebuild() if "--rebuild" in sys.argv[1:] else run())
