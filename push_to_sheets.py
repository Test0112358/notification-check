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

    # Rolling 7-day activity from the full changelog
    if os.path.exists("data/changelog.json"):
        with open("data/changelog.json") as f:
            log = json.load(f)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        recent_new, recent_changed = [], []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp_utc", "")
            try:
                when = datetime.datetime.fromisoformat(ts.replace("Z", ""))
            except (ValueError, AttributeError):
                continue
            if when < cutoff:
                continue
            when_aest = (when + datetime.timedelta(hours=10)).strftime("%d %b %H:%M")
            if entry.get("event") == "NEW_ENTRY":
                recent_new.append((entry.get("title", ""), entry.get("case_number", ""), when_aest))
            elif entry.get("event") == "CHANGED":
                for fc in entry.get("changes", []):
                    if isinstance(fc, dict):
                        recent_changed.append([
                            entry.get("title", ""), fc.get("field", ""),
                            f"{fc.get('old_value', '')} → {fc.get('new_value', '')}", when_aest
                        ])

        if recent_new:
            rows.append([f"NEW ENTRIES (LAST 7 DAYS) — {len(recent_new)}", "Case", "Detected (AEST)", ""])
            rows += [[t, cn, w, ""] for t, cn, w in recent_new]
            rows.append(["", "", "", ""])
        if recent_changed:
            rows.append([f"CHANGED (LAST 7 DAYS) — {len(recent_changed)} events", "Field", "Change", "When"])
            rows += recent_changed
            rows.append(["", "", "", ""])
    
    rows.append([
        "Register URL",
        "https://www.accc.gov.au/public-registers/mergers-and-acquisitions-registers/acquisitions-register",
        "", "",
    ])

    ws.clear()
    ws.update(rows)
    print(f"  Last Updated tab: refreshed ({len(rows)} rows)")

def push_dashboard(sh):
    try:
        ws = sh.worksheet("Dashboard")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Dashboard", rows=80, cols=4)

    df = pd.read_csv("data/register.csv", dtype=str).fillna("")
    today = datetime.datetime.utcnow() + datetime.timedelta(hours=10)

    def col(*names):
        return next((c for c in names if c in df.columns), None)

    title_c = col("Title", "title")
    case_c  = col("Case Number", "case_number")
    due_c   = col("End Of Determination Period", "end_of_determination_period")
    det_c   = col("ACCC Determination", "accc_determination")
    stage_c = col("Stage", "Acquisition Status", "stage")
    dec_c   = col("Decisions Docs", "decisions_docs")
    anz_c   = col("M&A Sector", "ANZSIC Codes", "anzsic_codes")

    # Decisions due (active, within 14 days, sorted by urgency)
    due_list = []
    if due_c:
        for _, r in df.iterrows():
            if det_c and str(r.get(det_c, "")).strip():
                continue
            ds = str(r.get(due_c, "")).strip()
            d = None
            for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    d = datetime.datetime.strptime(ds, fmt); break
                except ValueError:
                    continue
            if not d:
                continue
            days = (d - today).days
            if 0 <= days <= 14:
                cn = str(r.get(case_c, "")).strip() if case_c else ""
                due_list.append((days, str(r.get(title_c, "")).strip(), ds, cn))
    due_list.sort()

    # Active Phase 2 cases
    phase2 = []
    if dec_c:
        for _, r in df.iterrows():
            if "phase 2" in str(r.get(dec_c, "")).lower():
                if not (det_c and str(r.get(det_c, "")).strip()):
                    phase2.append(str(r.get(title_c, "")).strip())

    # Active cases with extended determination timelines
    extended = []
    if dec_c:
        for _, r in df.iterrows():
            if "timeline extended" in str(r.get(dec_c, "")).lower():
                if not (det_c and str(r.get(det_c, "")).strip()):
                    cn = str(r.get(case_c, "")).strip() if case_c else ""
                    extended.append((str(r.get(title_c, "")).strip(), cn))

    # Sector counts
    sectors = {}
    if anz_c == "M&A Sector":
        for v in df[anz_c]:
            for s in str(v).split(";"):
                s = s.strip()
                if s:
                    sectors[s] = sectors.get(s, 0) + 1
    top_sectors = sorted(sectors.items(), key=lambda x: -x[1])[:20]

    rows = [["ACCC REGISTER — DASHBOARD", today.strftime("%d %b %Y %H:%M AEST"), ""]]
    rows.append(["", "", ""])
    rows.append([f"DECISIONS DUE (next 14 days) — {len(due_list)}", "Case", "Due date", "Days left"])
    rows += [[t, cn, ds, dleft] for dleft, t, ds, cn in due_list] or [["None pending", "", "", ""]]
    rows.append(["", "", ""])
    rows.append([f"ACTIVE PHASE 2 REVIEWS — {len(phase2)}", "", ""])
    rows += [[p, "", ""] for p in phase2] or [["None active", "", ""]]
    rows.append(["", "", ""])
    rows.append([f"EXTENDED TIMELINES (active) — {len(extended)}", "Case", ""])
    rows += [[t, cn, ""] for t, cn in extended] or [["None", "", ""]]
    rows.append(["", "", ""])
    if top_sectors:
        rows.append(["TOP SECTORS BY DEAL COUNT", "Deals", ""])
        rows += [[s, n, ""] for s, n in top_sectors]

    ws.clear()
    ws.update(rows)
    ws.format("A1:C1", {"backgroundColor": {"red": 0.05, "green": 0.11, "blue": 0.16},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
    ws.format("A3:C3", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.93, "green": 0.95, "blue": 0.97}})
    ws.freeze(rows=1)
    print(f"  Dashboard: refreshed ({len(rows)} rows)")

def main():
    print("\nPushing data to Google Sheets...")
    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    push_raw_data(sh)
    push_summary(sh)
    push_dashboard(sh)
    print("  Done.")


if __name__ == "__main__":
    main()
