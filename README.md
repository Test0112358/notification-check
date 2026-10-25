# ACCC Acquisitions Register Monitor

Scrapes the ACCC Acquisitions Register on a schedule, detects field-level changes on every matter, downloads consultation documents, and sends readable HTML email alerts. Results are committed back to this repository, so `data/` is a version-controlled database and every commit is a timestamped snapshot.

---

## What you get

| Output | Where | When |
|---|---|---|
| **Register update email** (HTML, Excel attached) | Email | A run finds new matters, material changes, removals or new decision documents |
| **Consultation documents email** (only this run's files attached) | Email | New questionnaires, remedy offers or draft s87B undertakings are downloaded |
| **Watchlist alert** | Email | A matter matching `watchlist.json` is new, changes, or gets a new decision document |
| **Weekly digest** (Excel attached) | Email | First successful run of each week (normally Monday morning) |
| **Monitor failure alert** | Email | A run fails (site blocked, data check failed, crash). Repeats about daily while failing |
| **Verification failure alert** | Email | The independent accuracy checks find errors |
| Google Sheets tabs | Sheets | Every successful run (optional) |
| `data/register.csv`, `data/register.xlsx` | Repo / run artifact | Every successful run |

### Alerts only fire on material changes

Every change is recorded in `data/changelog.json`, tagged `"material": true/false`.

- **Material:** status, stage, determination, dates, parties, ANZSIC codes, consultation close date, and new rows in the document tables.
- **Cosmetic (recorded, never alerted):** the ACCC page's "last modified" time, consultation wording tweaks, a document link being filled in, and the consultation table disappearing after consultation closes.

### Day counts

All business and calendar day counts treat the **Effective Notification Date as Day 0**, so a standard Phase 1 period is 30 business days. Business days exclude weekends, ACT public holidays (official ACT Government lists, 2025 to 2029) and the ACCC's 23 December to 11 January suspension. The verifier recounts every period with a different algorithm and requires an exact match.

---

## Columns in `register.csv` / Raw Data

The original columns keep their positions. New columns are appended at the end, so existing column letters in your spreadsheets still line up.

| Column | Notes |
|---|---|
| Acquisition Name, Type, Case Number | |
| Acquirer(s), Target(s) / Vendor(s), Other Parties | One item per party, with its company number attached, e.g. `Smile Partners (NSW3) Pty Ltd (ACN 695 902 955)` |
| M&A Sector | Derived from ANZSIC codes (`anzsic_sectors.py`) |
| Notification / Application Date, End of Determination Period, Determination Publication Date | ISO dates (YYYY-MM-DD) |
| Determination Period Calendar Days / Business Days (ACT) | Day 0 = notification date |
| Elapsed Calendar / Business Days to Decision (ACT) | Notification date → determination publication date |
| Stage, Acquisition Status, ACCC Determination, ANZSIC Code(s), URL | |
| Decisions and Key Events, Consultation Text, Consultation Documents | |
| Questionnaire | `Y` if the matter had a consultation questionnaire |
| ACCC Page Last Modified, Last Checked (Canberra time) | Canberra time, AEST or AEDT as applicable |
| **Business Days Elapsed / Remaining, Deadline Status** | Deadline tracker for matters under assessment: On track, Due within 10 / 5 business days, Due today, Past deadline (awaiting publication) |
| **Consultation Closes** | Parsed from the consultation text |
| **Latest Key Event** | Most recent decision/key event, classified (e.g. "Moved to Phase 2", "Timeline extended") |
| **Watchlist Match** | Why the matter matches `watchlist.json` |
| **First Seen** | When the monitor first saw the matter |

---

## Files in `data/`

| File | Contents |
|---|---|
| `register.json` | Current state of every matter (full detail) |
| `register.csv` | Spreadsheet-ready export (see columns above) |
| `changelog.json` | Append-only log of every change, new entry and removal, with `material` flags |
| `latest_changes.json` | This run's changes, with a readable `summary` line per change |
| `summary.json` | Totals, decisions due, consultations closing, data health and analytics (by year, month, stage, sector) |
| `status.csv` | Content of the "Last Updated" Sheets tab |
| `health.json` | Last successful run, consecutive failures, last error, requests used |
| `documents_manifest.json` | Every consultation document ever seen, with download status |
| `consultation_docs/` | Downloaded consultation documents, named `MN-12345 - <original name>` |
| `new_documents.json` | Documents downloaded and decision documents found in the latest run |
| `verification_*.{json,csv}` | Accuracy verification results |

`register.xlsx` is emailed and kept as a run artifact for 90 days but not committed, to keep the repository small. Its tabs are Summary, Active Matters, Notifications, Waivers, Recent Changes, Case History, Analytics and Raw Data.

### Consultation documents are never lost

Every consultation document is recorded in `documents_manifest.json` as soon as it is seen. Downloads that fail are retried on later runs, up to 6 attempts. Documents beyond the per-run cap of 25 wait for the next run instead of being dropped. Word, PDF and other formats are all collected.

On its first run, the manifest also backfills documents that appear only in the changelog. This recovers 10 PDF consultation documents (9 remedy offers or draft s87B undertakings and 1 questionnaire) that the old `.docx`-only logic skipped.

---

## Request budget (why runs are light)

Each run fetches the ~15 listing pages, then only the case pages that may have changed:

- new matters, and matters whose listing values (status, stage, dates) changed
- every matter under assessment
- matters decided or modified in the last 21 days
- pages whose previous fetch failed
- a rolling sweep of the least-recently-checked pages, sized so every page is re-checked about weekly

That is roughly **120 to 170 requests per run, instead of about 830**. Verification re-parses the HTML saved during the same run, so it adds no requests. Requests are 1.5 seconds apart, retries skip 4xx errors, `Retry-After` is honoured, and the run stops after 3 consecutive `403` responses.

To re-check every page once, run the workflow manually with **full_refresh** ticked.

---

## If the ACCC site blocks the monitor (HTTP 403)

From 5 October 2026 the ACCC's CDN (Akamai) has answered every request from GitHub's runners with `403 Access Denied`, even for `robots.txt`. The block is based on where requests come from (cloud/datacenter IP ranges), not on what is requested, so code changes alone cannot fix it. The monitor now emails you when this happens, and the data shows how old it is.

Options, in order of effort:

1. **Self-hosted runner (no code change).** Install a GitHub Actions runner on an always-on machine with a normal business or home connection (Settings → Actions → Runners → New self-hosted runner). Then add the repository variable `RUNNER_LABEL` = `self-hosted` (Settings → Secrets and variables → Actions → Variables). The workflow runs there from the next scheduled run.
2. **Identify the monitor and ask to be allowed.** Add the repository variable `MONITOR_CONTACT` (an email address or URL). It is appended to the User-Agent, e.g. `ACCC-Register-Monitor/2.0 (...; contact: you@example.com)`. Then ask the ACCC to allow it.

---

## Setup

1. **Workflow permissions:** Settings → Actions → General → Workflow permissions → *Read and write permissions*.
2. **Email (optional):** add repository secrets `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_SENDER` and `MAIL_RECIPIENTS` (comma-separated). Mail is sent through the Brevo SMTP relay configured in the workflow. Without `MAIL_USERNAME`, email steps are skipped.
3. **Google Sheets (optional):** add `GOOGLE_SHEETS_CREDENTIALS` (service account JSON) and `GOOGLE_SHEET_ID`, and share the sheet with the service account. Tabs written: Raw Data (Do not edit), Last Updated, Dashboard, Active Matters, Case History, Summary, Monthly Stats. Numbers and dates are written as real values, so they sort and filter correctly.
4. **Watchlist (optional):** edit `watchlist.json` (instructions inside).
5. **Run it:** Actions → ACCC Register Monitor + Verify → Run workflow.

**Schedule:** Monday to Friday at 10:23am and 4:23pm AEST (11:23am and 5:23pm AEDT). GitHub can start scheduled runs late. To change the times, edit the cron line in `.github/workflows/monitor.yml` and `SCHEDULE_TEXT` in `accc_common.py`.

---

## Running locally

```bash
pip install -r requirements.txt
python -m unittest discover -s tests     # parser, calculation and pipeline tests (no network)
python scraper.py                        # scrape, detect changes, write data/ and outbox/
python verify.py                         # verify (add --live 5 to also spot-check 5 pages live)
```

---

## Code layout

| File | Role |
|---|---|
| `accc_common.py` | Paths, ACT holiday calendar, business days, Canberra time, polite HTTP client, run health |
| `scraper.py` | Listing and case page parsing, which pages to fetch, change detection, sanity guard |
| `documents.py` | Consultation document manifest and downloads, decision document classification, consultation close dates |
| `reporting.py` | CSV, Excel, summary/analytics, deadline tracker, watchlist, HTML emails |
| `verify.py` | Independent accuracy checks (consistency rules, cross-source, independent re-parse) |
| `push_to_sheets.py` | Google Sheets tabs |
| `anzsic_sectors.py` | ANZSIC code → M&A sector mapping |
| `tests/` | Unit and end-to-end tests with HTML fixtures (run before every scrape) |

---

## Safeguards and known limitations

- **No official API.** Everything is parsed from HTML. If the ACCC changes its page layout, the **sanity guard** refuses to save data that has lost key fields on more than 25% of pages, or a listing with fewer than 80% of known matters. Stored data is left untouched and you get a failure email. The parser lives in `parse_card()` and `parse_detail_page()` in `scraper.py`.
- **Test fixtures are modelled, not captured.** `tests/fixtures/` mirrors the page structure the parser expects. When you next have access, saving a real listing page and case page over the fixtures makes the tests stronger.
- **ACT holidays.** Add each new year from the ACT Government PDF to `accc_common.py` and raise `HOLIDAY_TABLE_LAST_YEAR`. The verifier warns when register dates pass the last year covered.
- **Christmas suspension.** The 23 December to 11 January exclusion follows the ACCC Merger Process Guidelines. Check it against the latest guidelines each year.
- **Unusual company identifiers.** Party lines are paired with their company numbers by pattern. A few rare overseas identifier formats may still appear as a separate item.
- **GitHub Actions minutes.** A typical run takes a few minutes, well inside the free tier.
