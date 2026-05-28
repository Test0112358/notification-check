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
        ws = sh.add_worksheet("Last Updated", rows=20, cols=3)

    aest = datetime.datetime.utcnow() + datetime.timedelta(hours=10)

    df = pd.read_csv("data/register.csv", dtype=str).fillna("")
    total = len(df)
    notifs = len(df[df["Type"] == "Notification"]) if "Type" in df.columns else ""
    waivers = len(df[df["Type"] == "Waiver"]) if "Type" in df.columns else ""

    rows = [
        ["Metric", "Value", ""],
        ["Last updated (UTC)",  datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), ""],
        ["Last updated (AEST)", aest.strftime("%Y-%m-%d %H:%M"), ""],
        ["", "", ""],
        ["Total entries",  total,   ""],
        ["Notifications",  notifs,  ""],
        ["Waivers",        waivers, ""],
        ["", "", ""],
        ["Auto-run schedule", "Mon–Fri, 10am and 4pm AEST", ""],
    ]
    ws.clear()
    ws.update(rows)
    print("  Last Updated tab: refreshed")

def main():
    print("\nPushing data to Google Sheets...")
    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    push_raw_data(sh)
    push_summary(sh)
    print("  Done.")

if __name__ == "__main__":
    main()
