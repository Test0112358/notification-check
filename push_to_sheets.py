#!/usr/bin/env python3
"""
Push the register to Google Sheets
==================================
Tabs written (existing tab names are kept so links and charts keep working):

  Raw Data (Do not edit)  every matter, same columns as data/register.csv
  Last Updated            data health and this run's changes (from data/status.csv)
  Dashboard               deadlines, Phase 2, extensions, consultations, watchlist
  Active Matters          deadline tracker for matters under assessment
  Case History            every material change, newest first
  Summary                 KPIs, outcomes by year, stage and sector
  Monthly Stats           filed / completed / outcomes per month, all years

Data is read from data/register.json and friends rather than re-parsed from
the CSV, so column renames can no longer silently empty a tab. Numbers are
written as numbers and dates as real dates (sortable and filterable).
"""

import csv
import datetime
import json
import os

import gspread
from google.oauth2.service_account import Credentials

import documents
import reporting
from accc_common import (
    CHANGELOG_JSON, REGISTER_JSON, REGISTER_URL, STATUS_CSV, SUMMARY_JSON,
    canberra_today, data_age_days, fmt_canberra, load_health,
    load_json, parse_date, utc_now_iso,
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_HISTORY_ROWS = 4000

NAVY = {"red": 0.05, "green": 0.11, "blue": 0.16}
WHITE = {"red": 1, "green": 1, "blue": 1}
LIGHT = {"red": 0.95, "green": 0.96, "blue": 0.98}
TONES = {
    "red":   ({"red": 1.0, "green": 0.89, "blue": 0.89}, {"red": 0.6, "green": 0.11, "blue": 0.11}),
    "amber": ({"red": 1.0, "green": 0.95, "blue": 0.78}, {"red": 0.57, "green": 0.25, "blue": 0.05}),
    "green": ({"red": 0.93, "green": 0.99, "blue": 0.96}, {"red": 0.02, "green": 0.37, "blue": 0.27}),
}
STATUS_TONE = {
    "Past deadline (awaiting publication)": "red",
    "Due today": "red",
    "Due within 5 business days": "amber",
    "Due within 10 business days": "amber",
    "On track": "green",
}
DATE_COLUMNS = {
    "Notification / Application Date", "End of Determination Period",
    "Determination Publication Date", "Consultation Closes", "End of period", "Closes",
}


def get_client():
    creds_json = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# Writing helpers
# ---------------------------------------------------------------------------

def sheet_date(value):
    """Google Sheets date serial number (days since 30 Dec 1899), or the value unchanged."""
    d = parse_date(value) if isinstance(value, str) and value else None
    if d:
        return (d - datetime.date(1899, 12, 30)).days
    return value


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Tab:
    """Collects rows plus formatting, then writes a worksheet in three API calls."""

    def __init__(self, name):
        self.name = name
        self.rows = []
        self.header_rows = []      # dark header bars
        self.section_rows = []     # bold light-grey section titles
        self.date_cells = []       # (row, col) zero-based
        self.tones = []            # (row, col, tone)
        self.widths = {}           # col index → pixels
        self.frozen_rows = 1

    def add(self, row):
        self.rows.append(list(row))
        return len(self.rows) - 1

    def title(self, text, note=""):
        self.header_rows.append(self.add([text, note]))

    def section(self, text, *cols):
        self.section_rows.append(self.add([text, *cols]))

    def blank(self):
        self.add([""])

    def table(self, headers, records):
        """records: list of dicts keyed by header."""
        self.section_rows.append(self.add(headers))
        for rec in records:
            r = len(self.rows)
            row = []
            for ci, h in enumerate(headers):
                v = rec.get(h, "")
                if h in DATE_COLUMNS and parse_date(v):
                    self.date_cells.append((r, ci))
                    v = sheet_date(v)
                if h in ("Deadline Status", "Status") and v in STATUS_TONE:
                    self.tones.append((r, ci, STATUS_TONE[v]))
                row.append(v)
            self.add(row)

    def write(self, sh):
        try:
            ws = sh.worksheet(self.name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(self.name, rows=100, cols=10)

        width = max((len(r) for r in self.rows), default=1)
        rows = [r + [""] * (width - len(r)) for r in self.rows] or [[""]]
        sid = ws.id

        # 1. size the grid and reset old formatting
        sh.batch_update({"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {
                    "rowCount": len(rows) + 5, "columnCount": max(width, 4),
                    "frozenRowCount": self.frozen_rows}},
                "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}},
            {"repeatCell": {"range": {"sheetId": sid}, "cell": {"userEnteredFormat": {}},
                            "fields": "userEnteredFormat"}},
        ]})
        # 2. values (RAW: text is never interpreted as a formula)
        ws.clear()
        ws.update(rows, "A1", value_input_option="RAW")

        # 3. formatting
        reqs = []

        def fmt(r, c0, c1, fmt_obj, fields):
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r + 1,
                          "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": fmt_obj}, "fields": fields}})

        reqs.append({"repeatCell": {
            "range": {"sheetId": sid},
            "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "CLIP",
                                           "textFormat": {"fontSize": 10}}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat)"}})
        for r in self.header_rows:
            fmt(r, 0, width, {"backgroundColor": NAVY,
                              "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11}},
                "userEnteredFormat(backgroundColor,textFormat)")
        for r in self.section_rows:
            fmt(r, 0, len(self.rows[r]), {"backgroundColor": LIGHT, "textFormat": {"bold": True}},
                "userEnteredFormat(backgroundColor,textFormat)")
        for r, c in self.date_cells:
            fmt(r, c, c + 1, {"numberFormat": {"type": "DATE", "pattern": "d mmm yyyy"}},
                "userEnteredFormat.numberFormat")
        for r, c, tone in self.tones:
            bg, fg = TONES[tone]
            fmt(r, c, c + 1, {"backgroundColor": bg, "textFormat": {"bold": True, "foregroundColor": fg}},
                "userEnteredFormat(backgroundColor,textFormat)")
        for c, px in self.widths.items():
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        if reqs:
            sh.batch_update({"requests": reqs})
        print(f"  {self.name}: {len(rows)} rows")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def raw_data_tab(rows):
    tab = Tab("Raw Data (Do not edit)")
    headers = list(rows[0].keys()) if rows else []
    tab.table(headers, rows)
    tab.header_rows, tab.section_rows = [0], []
    tab.widths = {0: 320, 3: 260, 4: 260, 6: 220}
    return tab


def health_line(health):
    age = data_age_days(health)
    if age is None:
        return "Data age unknown"
    if age >= 2:
        return f"WARNING: data is {age} days old (last successful update {fmt_canberra(health.get('last_success_utc'))})"
    return f"Up to date (last successful update {fmt_canberra(health.get('last_success_utc'))})"


def last_updated_tab(health):
    tab = Tab("Last Updated")
    tab.title("ACCC Acquisitions Register: Last Updated", health_line(health))
    tab.blank()
    if os.path.exists(STATUS_CSV):
        with open(STATUS_CSV, encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if row and row[0].isupper() and len(row[0]) > 3 and not any(row[1:]):
                    tab.section(*row)
                elif row and row[0] == "Metric":
                    tab.section(*row)
                else:
                    tab.add(row)
    tab.blank()
    tab.add(["Register URL", REGISTER_URL])
    tab.widths = {0: 340, 1: 420, 2: 260}
    return tab


def dashboard_tab(register, health, watchlist):
    tab = Tab("Dashboard")
    tab.title("ACCC REGISTER: DASHBOARD", fmt_canberra(utc_now_iso()))
    tab.add(["Data status", health_line(health)])
    tab.blank()

    due, overdue, closing = reporting.deadline_lists(register)
    tab.table(["Decisions due (next 10 business days)", "Case", "End of period", "Progress", "Status"],
              [{"Decisions due (next 10 business days)": e.get("title", ""), "Case": e.get("case_number", ""),
                "End of period": e.get("end_of_determination_period", ""), "Progress": info["label"],
                "Status": info["status"]} for _, e, info in due] or [{"Decisions due (next 10 business days)": "None"}])
    tab.blank()
    tab.table(["Past deadline, awaiting publication", "Case", "End of period"],
              [{"Past deadline, awaiting publication": e.get("title", ""), "Case": e.get("case_number", ""),
                "End of period": e.get("end_of_determination_period", "")} for _, e, _ in overdue]
              or [{"Past deadline, awaiting publication": "None"}])
    tab.blank()

    phase2 = [e for e in register.values() if e.get("acquisition_status") == "Under assessment"
              and "phase 2" in e.get("stage", "").lower()]
    tab.table(["Active Phase 2 reviews", "Case", "End of period", "Progress"],
              [{"Active Phase 2 reviews": e.get("title", ""), "Case": e.get("case_number", ""),
                "End of period": e.get("end_of_determination_period", ""),
                "Progress": reporting.deadline_info(e)["label"]} for e in phase2] or [{"Active Phase 2 reviews": "None"}])
    tab.blank()

    extended = []
    for e in register.values():
        if e.get("acquisition_status") != "Under assessment":
            continue
        days = sum(documents.timeline_extension_days(d.get("description", "")) or 0
                   for d in e.get("decisions_docs", []) or [])
        if days:
            extended.append({"Extended timelines (active)": e.get("title", ""), "Case": e.get("case_number", ""),
                             "Business days added": days,
                             "End of period": e.get("end_of_determination_period", "")})
    tab.table(["Extended timelines (active)", "Case", "Business days added", "End of period"],
              extended or [{"Extended timelines (active)": "None"}])
    tab.blank()

    tab.table(["Consultations closing (5 business days)", "Case", "Closes", "Business days left"],
              [{"Consultations closing (5 business days)": e.get("title", ""), "Case": e.get("case_number", ""),
                "Closes": close.isoformat(), "Business days left": left} for _, e, close, left in closing]
              or [{"Consultations closing (5 business days)": "None"}])
    tab.blank()

    watched = []
    for e in register.values():
        reasons = reporting.watchlist_matches(e, watchlist)
        if reasons and e.get("acquisition_status") == "Under assessment":
            watched.append({"Watchlist (active matters)": e.get("title", ""), "Case": e.get("case_number", ""),
                            "Matched on": "; ".join(reasons),
                            "Status": reporting.deadline_info(e)["status"]})
    tab.table(["Watchlist (active matters)", "Case", "Matched on", "Status"],
              watched or [{"Watchlist (active matters)": "None (edit watchlist.json to add companies, sectors or keywords)"}])
    tab.widths = {0: 420, 1: 110, 2: 160, 3: 160, 4: 230}
    return tab


def active_matters_tab(rows):
    tab = Tab("Active Matters")
    active = [r for r in rows if r["Acquisition Status"] == "Under assessment"]
    order = {"Past deadline (awaiting publication)": 0, "Due today": 1, "Due within 5 business days": 2,
             "Due within 10 business days": 3, "On track": 4}
    active.sort(key=lambda r: (order.get(r["Deadline Status"], 9),
                               r["Business Days Remaining"] if r["Business Days Remaining"] != "" else 999))
    for r in active:
        r["Progress"] = (f"Day {r['Business Days Elapsed']} of {r['Determination Period Business Days (ACT)']}"
                         if r["Business Days Elapsed"] != "" else "")
    headers = ["Acquisition Name", "Case Number", "Type", "Stage", "Notification / Application Date",
               "End of Determination Period", "Progress", "Business Days Remaining", "Deadline Status",
               "Consultation Closes", "Latest Key Event", "M&A Sector", "Watchlist Match", "URL"]
    tab.table(headers, active)
    tab.header_rows, tab.section_rows = [0], []
    tab.widths = {0: 340, 3: 190, 8: 230, 10: 260, 11: 220}
    return tab


def case_history_tab(changelog, register):
    tab = Tab("Case History")
    history = reporting.case_history_rows(changelog, register)[:MAX_HISTORY_ROWS]
    tab.table(["Detected (Canberra time)", "Case Number", "Matter", "Event", "Detail", "URL"], history)
    tab.header_rows, tab.section_rows = [0], []
    tab.widths = {0: 170, 2: 320, 3: 180, 4: 520}
    return tab


def _stat_row(label, s, extra=()):
    rate = f"{s['approval_rate_pct']}%" if s.get("approval_rate_pct") is not None else "-"
    return [label, *extra, s["determined"], s["approved"], s["approved_with_conditions"], s["not_approved"],
            rate, s["median_business_days"] if s["median_business_days"] is not None else "-",
            s["mean_business_days"] if s["mean_business_days"] is not None else "-"]


STAT_HEADERS = ["Determined", "Approved", "With conditions", "Not approved", "Approval rate",
                "Median business days", "Mean business days"]


def summary_tab(summary):
    a = summary["analytics"]
    tab = Tab("Summary")
    tab.title("ACCC ACQUISITIONS REGISTER: SUMMARY", fmt_canberra(summary.get("generated_at_utc")))
    tab.blank()
    tab.section("KPI SNAPSHOT", "Matters", *STAT_HEADERS)
    for kind in ("Notification", "Waiver"):
        s = a["by_type"].get(kind)
        if s:
            tab.add(_stat_row(kind + "s", s, (s["matters"],)))
    tab.add(["Under assessment", summary["by_acquisition_status"].get("Under assessment", 0)])
    tab.add(["Decisions due within 10 business days", len(summary["decisions_due_within_10_days"])])
    tab.add(["Past deadline, awaiting publication", len(summary["past_deadline_awaiting_publication"])])
    tab.blank()
    tab.section("BY YEAR", "Type", "Filed", "Completed", *STAT_HEADERS)
    for y in a["yearly"]:
        tab.add(_stat_row(y["year"], y, (y["type"], y["filed"], y["completed"])))
    tab.blank()
    tab.section("NOTIFICATIONS BY STAGE", "Matters", *STAT_HEADERS)
    for stage, s in a["by_stage"].items():
        tab.add(_stat_row(stage, s, (s["matters"],)))
    tab.blank()
    tab.section("BY M&A SECTOR", "Matters", "Under assessment", *STAT_HEADERS)
    for s in a["by_sector"]:
        tab.add(_stat_row(s["sector"], s, (s["matters"], s["under_assessment"])))
    tab.widths = {0: 300}
    return tab


def monthly_stats_tab(summary):
    tab = Tab("Monthly Stats")
    tab.title("ACCC REGISTER: MONTHLY STATISTICS", fmt_canberra(summary.get("generated_at_utc")))
    tab.blank()
    monthly = summary["analytics"]["monthly"]
    for kind in ("Notification", "Waiver"):
        tab.section(f"{kind.upper()}S", "Month", "Filed", "Completed", *STAT_HEADERS)
        for m in (m for m in monthly if m["type"] == kind):
            month = datetime.date(m["year"], m["month"], 1).strftime("%B")
            tab.add(_stat_row(m["year"], m, (month, m["filed"], m["completed"])))
        tab.blank()
    tab.widths = {0: 90, 1: 110}
    return tab


def main():
    print("\nPushing data to Google Sheets...")
    register = load_json(REGISTER_JSON, {})
    changelog = load_json(CHANGELOG_JSON, [])
    summary = load_json(SUMMARY_JSON, {})
    health = load_health()
    watchlist = reporting.load_watchlist()
    manifest = documents.load_manifest()
    rows = reporting.register_rows(register, manifest, watchlist, canberra_today())

    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    for tab in (
        raw_data_tab(rows),
        last_updated_tab(health),
        dashboard_tab(register, health, watchlist),
        active_matters_tab(rows),
        case_history_tab(changelog, register),
        summary_tab(summary),
        monthly_stats_tab(summary),
    ):
        tab.write(sh)
    print("  Done.")


if __name__ == "__main__":
    main()
