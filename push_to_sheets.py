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

def push_monthly_stats(sh):
    try:
        ws = sh.worksheet("Monthly Stats")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Monthly Stats", rows=40, cols=8)

    df = pd.read_csv("data/register.csv", dtype=str).fillna("")

    def col(*names):
        return next((c for c in names if c in df.columns), None)

    type_c = col("Type", "type")
    notif_c = col("Notification / Application Date", "Notification Date", "notification_date")
    det_c = col("ACCC Determination", "accc_determination")
    pub_c = col("Determination Publication Date", "Determination Date", "determination_publication_date")
    biz_c = col("Elapsed Business Days to Decision (ACT)", "Elapsed Business Days", "elapsed_business_days")

    def parse_d(s):
        s = str(s).strip()
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    year = 2026
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]

    def stats_block(subtype):
        sub = df[df[type_c] == subtype] if type_c else df
        block = [[f"{subtype.upper()}S - {year}", "", "", "", "", "", ""],
                 ["Month", "Total Filed", "Total Completed", "Approved",
                  "Not Approved", "Approval Rate", "Avg Business Days"]]
        tot = [0, 0, 0, 0]
        all_biz = []
        for m_idx, m_name in enumerate(months, 1):
            filed = completed = approved = notapproved = 0
            biz_days = []
            for _, r in sub.iterrows():
                nd = parse_d(r.get(notif_c, "")) if notif_c else None
                pd_ = parse_d(r.get(pub_c, "")) if pub_c else None
                det = str(r.get(det_c, "")).strip().lower() if det_c else ""
                if nd and nd.year == year and nd.month == m_idx:
                    filed += 1
                if pd_ and pd_.year == year and pd_.month == m_idx:
                    completed += 1
                    if det.startswith("approved"):
                        approved += 1
                    elif det:
                        notapproved += 1
                    b = str(r.get(biz_c, "")).strip() if biz_c else ""
                    try:
                        biz_days.append(float(b))
                    except ValueError:
                        pass
            rate = f"{approved / completed:.0%}" if completed else "-"
            avg = round(sum(biz_days) / len(biz_days), 1) if biz_days else "-"
            block.append([m_name, filed, completed, approved, notapproved, rate, avg])
            tot[0] += filed; tot[1] += completed; tot[2] += approved; tot[3] += notapproved
            all_biz += biz_days
        t_rate = f"{tot[2] / tot[1]:.0%}" if tot[1] else "-"
        t_avg = round(sum(all_biz) / len(all_biz), 1) if all_biz else "-"
        block.append(["Total", tot[0], tot[1], tot[2], tot[3], t_rate, t_avg])
        block.append(["", "", "", "", "", "", ""])
        return block

    rows = [["ACCC REGISTER - MONTHLY STATISTICS",
             (datetime.datetime.utcnow() + datetime.timedelta(hours=10)).strftime("%d %b %Y %H:%M AEST"),
             "", "", "", "", ""]]
    rows.append(["", "", "", "", "", "", ""])
    rows += stats_block("Notification")
    rows += stats_block("Waiver")

    ws.clear()
    ws.update(rows)
    ws.format("A1:G1", {"backgroundColor": {"red": 0.05, "green": 0.11, "blue": 0.16},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
    ws.freeze(rows=1)
    print(f"  Monthly Stats: refreshed ({len(rows)} rows)")

def push_summary_dashboard(sh):
    try:
        ws = sh.worksheet("Summary")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Summary", rows=45, cols=15)

    df = pd.read_csv("data/register.csv", dtype=str).fillna("")

    def col(*names):
        return next((c for c in names if c in df.columns), None)

    type_c  = col("Type", "type")
    notif_c = col("Notification / Application Date", "Notification Date")
    det_c   = col("ACCC Determination", "accc_determination")
    pub_c   = col("Determination Publication Date", "Determination Date")
    biz_c   = col("Elapsed Business Days to Decision (ACT)", "Elapsed Business Days")
    stage_c = col("Stage", "stage")
    sect_c  = col("M&A Sector")

    def parse_d(s):
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(str(s).strip(), fmt)
            except ValueError:
                continue
        return None

    def fnum(s):
        try:
            return float(str(s).strip())
        except ValueError:
            return None

    YEAR = 2026
    MONTHS = ["January","February","March","April","May","June",
              "July","August","September","October","November","December"]

    def monthly_table(subtype, heading):
        sub = df[df[type_c] == subtype]
        out = [[heading] + [""]*7,
               ["Month","Total Filed","Total Completed","Approved",
                "Not Approved","Approval Rate","Avg. Business Days","Trend"]]
        prev_avg, tot = None, [0,0,0,0]
        all_b = []
        for mi, mn in enumerate(MONTHS, 1):
            f = c = a = n = 0
            b = []
            for _, r in sub.iterrows():
                nd, pdt = parse_d(r.get(notif_c,"")), parse_d(r.get(pub_c,""))
                det = str(r.get(det_c,"")).strip().lower()
                if nd and (nd.year, nd.month) == (YEAR, mi): f += 1
                if pdt and (pdt.year, pdt.month) == (YEAR, mi):
                    c += 1
                    if det.startswith("approved"): a += 1
                    elif det: n += 1
                    v = fnum(r.get(biz_c,""))
                    if v is not None: b.append(v)
            avg = round(sum(b)/len(b),1) if b else None
            trend = "-" if avg is None or prev_avg is None else ("slower" if avg > prev_avg else "faster" if avg < prev_avg else "steady")
            out.append([mn, f, c, a, n,
                        f"{a/c:.0%}" if c else "-",
                        avg if avg is not None else "-", trend])
            if avg is not None: prev_avg = avg
            tot = [tot[0]+f, tot[1]+c, tot[2]+a, tot[3]+n]; all_b += b
        out.append(["Total", *tot,
                    f"{tot[2]/tot[1]:.0%}" if tot[1] else "-",
                    round(sum(all_b)/len(all_b),1) if all_b else "-", ""])
        return out, tot, all_b

    w_rows, w_tot, w_b = monthly_table("Waiver",       f"WAIVERS - {YEAR}")
    n_rows, n_tot, n_b = monthly_table("Notification", f"NOTIFICATIONS - {YEAR}")

    # KPI / pipeline panel
    stages = df[stage_c].str.lower() if stage_c else pd.Series(dtype=str)
    dets   = df[det_c].str.lower()   if det_c   else pd.Series(dtype=str)
    is_n   = df[type_c] == "Notification"
    is_w   = df[type_c] == "Waiver"
    ph2_active  = int((is_n & stages.str.contains("phase 2", na=False) & (dets == "")).sum())
    pending     = int((is_n & (dets == "")).sum())
    all_b2 = sorted(w_b + n_b)
    med = all_b2[len(all_b2)//2] if all_b2 else "-"
    comb_c, comb_a = w_tot[1]+n_tot[1], w_tot[2]+n_tot[2]
    kpi = [
        ("KPI SNAPSHOT", ""),
        ("Waivers completed",  w_tot[1]), ("Waivers approved", w_tot[2]),
        ("Waiver approval rate", f"{w_tot[2]/w_tot[1]:.0%}" if w_tot[1] else "-"),
        ("Notifs completed",   n_tot[1]), ("Notifs approved",  n_tot[2]),
        ("Notif approval rate", f"{n_tot[2]/n_tot[1]:.0%}" if n_tot[1] else "-"),
        ("Combined approval rate", f"{comb_a/comb_c:.0%}" if comb_c else "-"),
        ("Total matters on register", len(df)),
        ("", ""),
        ("SCRUTINY & PIPELINE", ""),
        ("Phase 2 notifs (active)", ph2_active),
        ("Notifs pending decision", pending),
        ("Fastest processed (days)", min(all_b2) if all_b2 else "-"),
        ("Slowest processed (days)", max(all_b2) if all_b2 else "-"),
        ("Median processing (days)", med),
    ]

    # Industry breakdown from M&A Sector
    ind = [("INDUSTRY BREAKDOWN", "")]
    if sect_c:
        counts = {}
        for v in df[sect_c]:
            for s in str(v).split(";"):
                s = s.strip()
                if s: counts[s] = counts.get(s, 0) + 1
        ind += sorted(counts.items(), key=lambda x: -x[1])[:15]

    left = [["ACCC ACQUISITIONS REGISTER - SUMMARY DASHBOARD"] + [""]*7, [""]*8] \
           + w_rows + [[""]*8] + n_rows
    rows = []
    for i in range(max(len(left), len(kpi)+2, len(ind)+2)):
        row = (left[i] if i < len(left) else [""]*8)[:8] + [""]*(8-len(left[i] if i < len(left) else []))
        k = kpi[i-2] if 2 <= i-0 and (i-2) < len(kpi) else ("", "")
        d = ind[i-2] if 2 <= i-0 and (i-2) < len(ind) else ("", "")
        rows.append(row + ["", k[0], k[1], "", d[0], d[1]])

    ws.clear()
    ws.update(rows)
    ws.format("A1:N1", {"backgroundColor": {"red": 0.05, "green": 0.11, "blue": 0.16},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
    ws.freeze(rows=1)
    print(f"  Summary dashboard: refreshed ({len(rows)} rows)")

def main():
    print("\nPushing data to Google Sheets...")
    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    push_raw_data(sh)
    push_summary(sh)
    push_dashboard(sh)
    push_summary_dashboard(sh)
    push_monthly_stats(sh)
    print("  Done.")


if __name__ == "__main__":
    main()
