"""
push_to_sheets.py
Pushes the current register data directly into Google Sheets via the API.
Replaces IMPORTDATA — no TOS risk, updates on every scrape run.
"""

import os
import json
import datetime
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_client():
    creds_json = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    return gspread.authorize(creds)

def push_raw_data(sh):
    df = pd.read_csv("data/register.csv", dtype=str).fillna("")
    ws = sh.worksheet("Raw Data (Do not edit)")
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())
    print(f"  Raw Data: pushed {len(df)} rows")

def push_summary(sh):
    try:
        ws = sh.worksheet("Last Updated")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Last Updated", rows=50, cols=4)

    aest = datetime.datetime.utcnow() + datetime.timedelta(hours=10)

    df = pd.read_csv("data/register.csv", dtype=str).fillna("")
    total = len(df)
    notifs = len(df[df["Type"] == "Notification"]) if "Type" in df.columns else 0
    waivers = len(df[df["Type"] == "Waiver"]) if "Type" in df.columns else 0

    # Load summary.json for change counts
    summary = {}
    if os.path.exists("data/summary.json"):
        with open("data/summary.json") as f:
            summary = json.load(f)

    new_count     = summary.get("new_count", 0)
    changed_count = summary.get("changed_count", 0)
    removed_count = summary.get("removed_count", 0)

    # Load latest_changes.json for detail
    changes = []
    if os.path.exists("data/latest_changes.json"):
        with open("data/latest_changes.json") as f:
            changes = json.load(f)

    # Load status.csv for decisions due
    decisions_due = []
    if os.path.exists("data/status.csv"):
        status_df = pd.read_csv("data/status.csv", dtype=str).fillna("")
        if "decision_due" in status_df.columns:
            due = status_df[status_df["decision_due"].str.strip() != ""]
            for _, row in due.iterrows():
                decisions_due.append(
                    f"{row.get('title', '')} — {row.get('decision_due', '')}"
                )

    rows = [
        ["ACCC Acquisitions Register — Last Updated", "", "", ""],
        ["", "", "", ""],
        ["Last updated (UTC)",  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "", ""],
        ["Last updated (AEST)", aest.strftime("%Y-%m-%d %H:%M"), "", ""],
        ["Auto-run schedule",   "Mon–Fri, 10am and 4pm AEST", "", ""],
        ["", "", "", ""],
        ["REGISTER TOTALS", "", "", ""],
        ["Total entries",   total,   "", ""],
        ["Notifications",   notifs,  "", ""],
        ["Waivers",         waivers, "", ""],
        ["", "", "", ""],
        ["CHANGES IN THIS RUN", "", "", ""],
        ["New entries",     new_count,     "", ""],
        ["Changed entries", changed_count, "", ""],
        ["Removed entries", removed_count, "", ""],
        ["", "", "", ""],
    ]

   if changes:
        rows.append(["CHANGE DETAIL", "", "", ""])
        rows.append(["Case", "Event", "Field", "Change"])
        for c in changes:
            # Guard against non-dict entries in the JSON
            if not isinstance(c, dict):
                rows.append([str(c), "", "", ""])
                continue
            title = c.get("title", c.get("slug", ""))
            event = c.get("event", "")
            field_changes = c.get("changes", [])
            if field_changes:
                for fc in field_changes:
                    if isinstance(fc, dict):
                        rows.append([
                            title, event,
                            fc.get("field", ""),
                            f"{fc.get('old_value', '')} → {fc.get('new_value', '')}"
                        ])
                    else:
                        rows.append([title, event, str(fc), ""])
            else:
                rows.append([title, event, "", ""])
        rows.append(["", "", "", ""])

    if decisions_due:
        rows.append(["DECISIONS DUE WITHIN 10 DAYS", "", "", ""])
        for d in decisions_due:
            rows.append([d, "", "", ""])
        rows.append(["", "", "", ""])

    rows.append(["Register URL", "https://www.accc.gov.au/public-registers/mergers-and-acquisitions-registers/acquisitions-register", "", ""])

    ws.clear()
    ws.update(rows)
    print(f"  Last Updated tab: refreshed ({len(rows)} rows)")

def main():
    print("\nPushing data to Google Sheets...")
    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    push_raw_data(sh)
    push_summary(sh)
    print("  Done.")

if __name__ == "__main__":
    main()
