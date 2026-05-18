ACCC Acquisitions Register Monitor
Automatically scrapes the ACCC Acquisitions Register, detects any field-level changes across all 250+ entries (including granular date changes on individual case pages), and commits the results back to this repository on a schedule.
The `data/` directory is a version-controlled database. Every git commit is a timestamped snapshot. The full history of every change is in `data/changelog.json`.
---
What is tracked
For every entry on the register:
Column	Source
Acquisition Name	Detail page h1
Type	Notification / Waiver
Case Number	MN-XXXXX or WA-XXXXX
Acquirer(s)	Detail page (semicolon-separated)
Target(s) / Vendor(s)	Detail page (semicolon-separated)
Other Parties	Detail page (semicolon-separated)
Notification / Application Date	Listing + detail page
Stage	Phase 1, Phase 2, Waiver application, etc.
Acquisition Status	Under assessment, Assessment completed, etc.
End of Determination Period	Detail page only (notification cases)
Determination Period Calendar Days (ACT)	Calculated: notification date → end of det. period
Determination Period Business Days (ACT)	Calculated using ACT public holiday calendar
Elapsed Calendar Days to Decision	Calculated: notification date → det. publication date
Elapsed Business Days to Decision (ACT)	Same, ACT business days
ACCC Determination	Approved / Approved with conditions / Not approved
Determination Publication Date	Detail page only (completed cases)
ANZSIC Code(s)	Detail page
URL	Direct link to the ACCC case page
ACCC Page Last Modified	`article:modified_time` meta tag — changes whenever ACCC edits the page
Last Scraped (UTC)	Timestamp of the scrape run
Change detection is field-level. Even a date that shifts by one day on an otherwise unchanged page will be caught.
---
Files in `data/`
File	Contents
`register.json`	Complete current state of every entry (full detail)
`register.csv`	Same data as above, as a spreadsheet-ready CSV
`changelog.json`	Append-only log of every change, new entry, and removal with timestamps
`summary.json`	Aggregate statistics (by status, stage, type, determinations; entries due within 10 days)
`latest_changes.json`	Only the changes from the most recent run — used for notifications
---
Setup
1. Fork or clone this repository
Create a private repository on GitHub (recommended — keeps your monitoring internal).
2. Enable GitHub Actions write permissions
Go to your repository → Settings → Actions → General → Workflow permissions.
Select Read and write permissions and save.
3. Set up optional notifications
All notifications are optional. If you skip this step, the scraper still runs and commits data — you just won't get push alerts.
Email (Gmail):
Create a Gmail App Password (not your main password).
Add these GitHub repository secrets (Settings → Secrets and variables → Actions → New repository secret):
`MAIL_USERNAME` — your Gmail address
`MAIL_PASSWORD` — the App Password
`MAIL_RECIPIENTS` — comma-separated list of recipient addresses
Slack:
Create an Incoming Webhook for your Slack workspace.
Add `SLACK_WEBHOOK_URL` as a GitHub repository secret.
If neither secret is set, those steps in the workflow are silently skipped.
4. Run it manually to confirm it works
Go to Actions → ACCC Register Monitor → Run workflow.
The first run will take ~5–8 minutes (fetching all ~250+ detail pages). Subsequent runs are the same speed since we always fetch all detail pages to catch granular changes.
5. Check the schedule
The workflow runs Monday–Friday at approximately 8 am, 11 am, 2 pm, and 5 pm AEST.
Edit `.github/workflows/monitor.yml` to adjust the cron schedule if needed.
---
Sharing data with your team
Download the CSV directly from GitHub:
```
https://raw.githubusercontent.com/YOUR-ORG/YOUR-REPO/main/data/register.csv
```
Team members can import this URL directly into Excel (Data → From Web) or Google Sheets
(`=IMPORTDATA("https://raw.githubusercontent.com/...")`), and it will always reflect the latest scrape.
GitHub repository access:
Add team members as collaborators (Settings → Collaborators). They can watch the repo to receive
GitHub notifications on every commit (i.e., every time changes are detected).
Actions artifacts:
Every run uploads the CSV, summary, and latest_changes files as a GitHub Actions artifact,
retained for 90 days. Team members can download these from the Actions tab without touching the repo.
---
Import into Google Sheets (live link)
In a Google Sheet cell, enter:
```
=IMPORTDATA("https://raw.githubusercontent.com/YOUR-ORG/YOUR-REPO/main/data/register.csv")
```
This imports a live copy. It refreshes when you reload the sheet (note: Google caches it for ~1 hour).
For a completely live feed, use a Google Apps Script trigger to re-import on a schedule.
---
Running locally
```bash
pip install -r requirements.txt
python scraper.py
```
The scraper reads from and writes to `data/` in the current directory.
On the first run there is no stored data, so everything is treated as new. This is expected.
---
Known limitations and honest caveats
No official API. The ACCC does not provide a structured data API. Everything is scraped
from HTML. If the ACCC redesigns their website (Drupal CMS upgrade, template change, etc.),
the field parser in `scraper.py` may need updating. The most fragile parts are the h3-label
parsing in `parse_detail_page()` and the card-text parsing in `parse_card()`.
ACCC Determination Period field. The ACCC website does not publish a field with this
exact name. The "Determination Period Calendar Days" and "Determination Period Business Days"
columns are calculated by this tool from notification date → end of determination period.
For completed cases, the elapsed columns use notification date → determination publication date.
ACT public holiday accuracy. Business day calculations use the `workalendar` library's
`AustralianCapitalTerritory` calendar. This covers standard ACT public holidays
(Canberra Day, Family & Community Day, etc.) but may not reflect ad hoc proclaimed holidays
or any holidays added after the library's last update. Verify calculations for edge cases.
Rate limiting. The scraper sends roughly 250–300 HTTP requests per run (one per case page).
At the current 1.2-second delay, each run takes ~5–8 minutes. If the ACCC blocks the scraper's
IP, increase `REQUEST_DELAY` in `scraper.py`. The User-Agent string identifies the tool honestly.
Detail-only fields. "End of determination period" and "ACCC Determination" only appear on
individual case pages, not on the listing. This is why we fetch every detail page every run
rather than only fetching changed pages. A date that shifts silently on a detail page will
still be caught because we compare `last_modified_accc` (the Drupal page modification timestamp)
as a tracked field in addition to the parsed values.
Verification. The scraper is not infallible. Spot-check the CSV against the ACCC website
periodically, especially for recently completed cases. The `last_modified_accc` column tells you
when the ACCC last touched each page, which helps confirm the data is current.
GitHub Actions free tier. At 4 runs/weekday × ~8 min/run × 22 weekdays/month ≈ 700 min/month.
Well within the 2,000 minutes/month free limit. If you switch to hourly runs, reassess.
---
Troubleshooting
Parser returns empty fields. The ACCC may have changed the HTML structure of a field label.
Open the relevant case page in your browser, right-click → Inspect, and check how the h3
headings and their values are structured. Update the `SCALAR_FIELDS` or `LIST_FIELDS` dicts
in `scraper.py` to match.
Scraper detects spurious changes every run. Likely a whitespace or encoding difference.
The `clean()` function normalises whitespace but cannot catch every edge case.
Inspect the `"old"` and `"new"` values in `changelog.json` to identify the pattern,
then add normalisation for that specific field.
Workflow fails with "Permission denied" on git push. Check that workflow write permissions
are enabled in repository settings (step 2 above).
---
Adapting for other ACCC registers
The `BASE_URL` and `REGISTER_PATH` constants in `scraper.py` and the `SCALAR_FIELDS` /
`LIST_FIELDS` dicts in `parse_detail_page()` are the only things you need to change to
monitor a different ACCC public register with a similar Drupal structure.
