#!/usr/bin/env python3
"""
Consultation documents and ACCC decision documents
==================================================
Consultation documents (questionnaires, remedy offers, draft s87B undertakings)
are downloaded to data/consultation_docs/ and tracked in
data/documents_manifest.json, keyed by download URL.

The manifest is what guarantees nothing is lost:
  - every consultation document ever seen is recorded as "pending" until a
    download succeeds, so documents beyond the per-run cap are simply picked
    up on the next run (previously they were silently dropped)
  - failed downloads are retried on later runs, up to MAX_DOWNLOAD_ATTEMPTS
  - filenames are prefixed with the case number so two generic names such as
    "Questionnaire.docx" can never overwrite each other
  - Word, PDF and other document types are all collected (previously .docx only)

Decision documents (determinations, Notices of Competition Concerns, timeline
extensions, etc.) are not downloaded; they are classified for alerts.
"""

import datetime
import hashlib
import os
import re
import shutil
import urllib.parse

from accc_common import (
    CHANGELOG_JSON, CONSULTATION_DOCS_DIR, DOCUMENTS_MANIFEST_JSON, OUTBOX_DIR,
    clean, load_json, parse_date, save_json, utc_now_iso,
)

MAX_DOWNLOADS_PER_RUN = 25
MAX_DOWNLOAD_ATTEMPTS = 6
DOCUMENT_EXTENSIONS = (".docx", ".doc", ".pdf", ".rtf", ".odt", ".xlsx", ".xls")

# Emails above this total attachment size list the remaining files as links.
MAX_EMAIL_ATTACHMENT_BYTES = 15 * 1024 * 1024

OUTBOX_ATTACHMENTS_DIR = os.path.join(OUTBOX_DIR, "attachments")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest():
    return load_json(DOCUMENTS_MANIFEST_JSON, {})


def save_manifest(manifest):
    save_json(DOCUMENTS_MANIFEST_JSON, manifest)


def _doc_ext(url):
    path = urllib.parse.urlparse(url).path.lower()
    for ext in DOCUMENT_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return ""


def legacy_filename(url):
    """Filename the pre-manifest scraper would have used (for matching old files)."""
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    return re.sub(r"[^\w\-_\. ]", "_", name)


def build_filename(url, case_number, slug):
    """'MN-12345 - Questionnaire.docx' style: readable, unique per matter."""
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    name = re.sub(r"[^\w\-_\. ()&,']", "_", name)
    name = clean(name.replace("_", " ")).strip(" .")
    if not name:
        name = "document" + (_doc_ext(url) or "")
    prefix = case_number or slug[:40]
    if not name.lower().startswith(prefix.lower()):
        name = f"{prefix} - {name}"
    return name


def register_document(manifest, url, entry, doc, source):
    """Add a consultation document to the manifest if it is not already tracked."""
    if not url or url in manifest or not _doc_ext(url):
        return False
    manifest[url] = {
        "url": url,
        "slug": entry.get("slug", ""),
        "case_number": entry.get("case_number", ""),
        "title": entry.get("title", ""),
        "accc_url": entry.get("url", ""),
        "date": doc.get("date", ""),
        "description": doc.get("description", ""),
        "kind": classify_consultation_document(doc.get("description", ""), url),
        "status": "pending",
        "attempts": 0,
        "first_seen_utc": utc_now_iso(),
        "source": source,
        "filename": "",
        "size_kb": None,
        "downloaded_utc": "",
        "last_error": "",
    }
    return True


def sync_manifest(manifest, register, run_utc):
    """
    Record every consultation document on the current register. On the first
    run with a manifest, also backfill:
      - documents already on disk (marked downloaded, keeping their filename)
      - documents only mentioned in the changelog (consultation sections are
        removed from ACCC pages once consultation closes)
    Returns the number of newly tracked documents.
    """
    added = 0
    first_run = not manifest

    for slug, entry in register.items():
        for doc in entry.get("consultation_docs", []) or []:
            if register_document(manifest, doc.get("url", ""), entry, doc, "register"):
                added += 1

    if first_run:
        added += _backfill_from_changelog(manifest, register)
        existing = set(os.listdir(CONSULTATION_DOCS_DIR)) if os.path.isdir(CONSULTATION_DOCS_DIR) else set()
        for url, rec in manifest.items():
            old_name = legacy_filename(url)
            if old_name in existing:
                path = os.path.join(CONSULTATION_DOCS_DIR, old_name)
                rec.update({
                    "status": "downloaded",
                    "filename": old_name,
                    "size_kb": round(os.path.getsize(path) / 1024, 1),
                    "downloaded_utc": "before manifest",
                    "source": rec["source"],
                })
    return added


def _backfill_from_changelog(manifest, register):
    changelog = load_json(CHANGELOG_JSON, [])
    added = 0
    pattern = re.compile(r"'url': '([^']+)'")
    desc_pattern = re.compile(r"'date': '([^']*)', 'description': '([^']*)', 'url': '([^']+)'")
    for event in changelog:
        for ch in event.get("changes", []):
            if ch.get("field") != "consultation_docs":
                continue
            for value in (ch.get("old", ""), ch.get("new", "")):
                described = {m.group(3): (m.group(1), m.group(2)) for m in desc_pattern.finditer(value)}
                for url in pattern.findall(value):
                    slug = event.get("slug", "")
                    entry = register.get(slug, {"slug": slug, "title": event.get("title", slug),
                                                "url": event.get("url", "")})
                    date, desc = described.get(url, ("", ""))
                    if register_document(manifest, url, entry, {"date": date, "description": desc},
                                         "changelog backfill"):
                        added += 1
    return added


def pending_documents(manifest):
    docs = [
        rec for rec in manifest.values()
        if rec["status"] in ("pending", "failed") and rec["attempts"] < MAX_DOWNLOAD_ATTEMPTS
    ]
    return sorted(docs, key=lambda r: r["first_seen_utc"])


def download_pending(manifest, session, run_utc, limit=MAX_DOWNLOADS_PER_RUN):
    """
    Download up to `limit` pending documents. Anything beyond the limit stays
    pending for the next run. Returns the list of manifest records downloaded
    in this run.
    """
    os.makedirs(CONSULTATION_DOCS_DIR, exist_ok=True)
    taken = {rec["filename"] for rec in manifest.values() if rec.get("filename")}
    downloaded = []

    queue = pending_documents(manifest)
    if len(queue) > limit:
        print(f"  {len(queue)} documents pending; downloading {limit} now, "
              f"{len(queue) - limit} will be fetched on later runs.")

    for rec in queue[:limit]:
        url = rec["url"]
        filename = build_filename(url, rec.get("case_number", ""), rec.get("slug", ""))
        if filename in taken:
            stem, ext = os.path.splitext(filename)
            filename = f"{stem} [{hashlib.sha1(url.encode()).hexdigest()[:6]}]{ext}"
        rec["attempts"] += 1
        print(f"  Downloading: {filename}")
        result = session.get(url, timeout=60, binary=True)
        if not result.ok or not result.text:
            rec["status"] = "failed"
            rec["last_error"] = result.error or f"HTTP {result.status}"
            print(f"  WARNING: Could not download {url}: {rec['last_error']} "
                  f"(attempt {rec['attempts']}/{MAX_DOWNLOAD_ATTEMPTS})")
            continue
        path = os.path.join(CONSULTATION_DOCS_DIR, filename)
        with open(path, "wb") as f:
            f.write(result.text)
        taken.add(filename)
        rec.update({
            "status": "downloaded",
            "filename": filename,
            "size_kb": round(len(result.text) / 1024, 1),
            "downloaded_utc": run_utc,
            "last_error": "",
        })
        downloaded.append(rec)
        print(f"  Saved: {path} ({rec['size_kb']} KB)")
    return downloaded


def stage_attachments(records):
    """
    Copy this run's downloads into the outbox for the email step, up to the
    attachment size budget. Returns (attached, linked_only) record lists.
    """
    if os.path.isdir(OUTBOX_ATTACHMENTS_DIR):
        shutil.rmtree(OUTBOX_ATTACHMENTS_DIR)
    os.makedirs(OUTBOX_ATTACHMENTS_DIR, exist_ok=True)
    attached, linked, total = [], [], 0
    for rec in records:
        src = os.path.join(CONSULTATION_DOCS_DIR, rec["filename"])
        size = os.path.getsize(src) if os.path.exists(src) else 0
        if size and total + size <= MAX_EMAIL_ATTACHMENT_BYTES:
            shutil.copy2(src, os.path.join(OUTBOX_ATTACHMENTS_DIR, rec["filename"]))
            attached.append(rec)
            total += size
        else:
            linked.append(rec)
    return attached, linked


def manifest_counts(manifest):
    counts = {"downloaded": 0, "pending": 0, "failed": 0, "gave_up": 0}
    for rec in manifest.values():
        if rec["status"] == "downloaded":
            counts["downloaded"] += 1
        elif rec["attempts"] >= MAX_DOWNLOAD_ATTEMPTS:
            counts["gave_up"] += 1
        else:
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_consultation_document(description, url=""):
    text = f"{description} {urllib.parse.unquote(url)}".lower()
    if "questionnaire" in text:
        return "Questionnaire"
    if "s87b" in text or "undertaking" in text:
        return "Draft s87B undertaking"
    if "remedy" in text:
        return "Remedy offer"
    if "phase 2 notice" in text or "notice of competition" in text:
        return "Phase 2 notice"
    return "Consultation document"


# (category, priority) — priority documents are highlighted in alerts.
_DECISION_RULES = [
    (r"not approved|not to approve|refus", "Determination: not approved", True),
    (r"with conditions|conditional", "Determination: approved with conditions", True),
    (r"notice of competition concern", "Notice of Competition Concerns", True),
    (r"subject to phase 2|phase 2 review|phase 2 notice|phase 2 referral", "Moved to Phase 2", True),
    (r"s87b|undertaking", "s87B undertaking", True),
    (r"remedy", "Remedy offer", True),
    (r"ceased", "Assessment ceased", True),
    (r"public benefit", "Public benefit application", True),
    (r"timeline extended|extension", "Timeline extended", False),
    (r"phase 2 determination", "Phase 2 determination", True),
    (r"waiver determination", "Waiver determination", False),
    (r"phase 1 determination|determination", "Phase 1 determination", False),
]


def classify_decision_document(description):
    """Return (category, is_priority) for a 'Decisions and key events' row."""
    text = clean(description).lower()
    for pattern, category, priority in _DECISION_RULES:
        if re.search(pattern, text):
            return category, priority
    return "Key event", False


def timeline_extension_days(description):
    m = re.search(r"extended by (\d+) business days?", description or "", re.I)
    return int(m.group(1)) if m else None


def doc_key(doc):
    """Identity of a document row independent of whether its URL is filled in yet."""
    return (clean(doc.get("date", "")).lower(), clean(doc.get("description", "")).lower())


def new_documents(old_docs, new_docs):
    """Rows present in new_docs and absent from old_docs (by date + description)."""
    old_keys = {doc_key(d) for d in old_docs or []}
    return [d for d in new_docs or [] if doc_key(d) not in old_keys]


# ---------------------------------------------------------------------------
# Consultation close date
# ---------------------------------------------------------------------------
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_DATE_RE = rf"(\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}})"
_CLOSE_PATTERNS = [
    # "...should be provided by 12 October 2026 via email..."
    rf"(?:provided|submitted|lodged|received|made)\s+(?:to the ACCC\s+)?by\s+(?:[^.]{{0,40}}?\s)?{_DATE_RE}",
    # "The ACCC is inviting submissions from interested parties on the Acquisitions by 16 June 2026"
    rf"submissions?\b[^.]{{0,160}}?\bby\s+(?:[^.]{{0,40}}?\s)?{_DATE_RE}",
    # "closes on 12 October 2026" / "due 12 October 2026"
    rf"(?:closes?|close|due|deadline)\s+(?:on\s+|is\s+|by\s+)?(?:[^.]{{0,30}}?\s)?{_DATE_RE}",
]


def parse_consultation_close_date(text):
    """Return the consultation close date as YYYY-MM-DD, or '' if none is stated."""
    if not text or "has concluded" in text.lower():
        return ""
    for pattern in _CLOSE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            d = parse_date(m.group(1))
            if d:
                return d.isoformat()
    return ""


def consultation_is_open(entry, today=None):
    close = parse_date(entry.get("consultation_close_date", ""))
    today = today or datetime.date.today()
    return bool(close and close >= today)
