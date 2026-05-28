import os
import json
import datetime
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


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

    summary = {}
    if os.path.exists("data/summary.json"):
        with open("data/summary.json") as f:
            summary = json.load(f)

    new_count = summary.get("new_count", 0)
    changed_count = summary.get("changed_count", 0)
    removed_count = summary.get("removed_count", 0)

    changes = []
    if os.path.exists("data/latest_changes.json"):
        with open("data/latest_changes.json") as f:
            content = json.load(f)
            if isinstance(content, list):
                changes = content
            elif isinstance(content, dict):
                changes = content.get("changes", [])

    decisions_due = []
    today_aest = datetime.datetime.utcnow() + datetime.timedelta(hours=10)
    title_col = "Title" if "Title" in df.columns else "title"
    due_col = next((c for c in ["End Of Determination Period", "end_of_determination_period"] if c in df.columns), None)
    det_col = next((c for c in ["ACCC Determination", "accc_determination"] if c in df.columns), None)
    if due_col:
        for _, row in df.iterrows():
            due_str = str(row.get(due_col, "")).strip()
            if not due_str:
                continue
            determination = str(row.get(det_col, "")).strip() if det_col else ""
            if determination:
                continue
            due_date = None
            for fmt in ["%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"]:
                try:
                    due_date = datetime.datetime.strptime(due_str, fmt)
                    break
                except ValueError:
                    continue
            if not due_date:
                continue
            days_remaining = (due_date - today_aest).days
            if 0 <= days_remaining <= 10:
                title = str(row.get(title_col, "")).strip()
                decisions_due.append(f"{title} — {due_str} ({days_remaining}d remaining)")

    rows = [
        ["ACCC Acquisitions Register — Last Updated", "", "", ""],
        ["", "", "", ""],
        ["Last updated (UTC)", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "", ""],
        ["Last updated (AEST)", aest.strftime("%Y-%m-%d %H:%M"), "", ""],
        ["Auto-run schedule", "Mon–Fri, 10am and 4pm AEST", "", ""],
        ["", "", "", ""],
        ["REGISTER TOTALS", "", "", ""],
        ["Total entries", total, "", ""],
        ["Notifications", notifs, "", ""],
        ["Waivers", waivers, "", ""],
        ["", "", "", ""],
        ["CHANGES IN THIS RUN", "", "", ""],
        ["New entries", new_count, "", ""],
        ["Changed entries", changed_count, "", ""],
        ["Removed entries", removed_count, "", ""],
        ["", "", "", ""],
    ]

    if changes:
        rows.append(["CHANGE DETAIL", "", "", ""])
        rows.append(["Case", "Event", "Field", "Change"])
        for c in changes:
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
                            title,
                            event,
                            fc.get("field", ""),
                            f"{fc.get('old_value', '')} → {fc.get('new_value', '')}",
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

    rows.append([
        "Register URL",
        "https://www.accc.gov.au/public-registers/mergers-and-acquisitions-registers/acquisitions-register",
        "", "",
    ])

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
