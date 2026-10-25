"""Reporting outputs and the Google Sheets writer (against a fake spreadsheet)."""

import datetime
import os
import unittest

import helpers
import push_to_sheets
import reporting
import scraper
from accc_common import REGISTER_JSON, load_json
from helpers import FakeRequestsSession, site_pages


class FakeWorksheet:
    def __init__(self, title, sheet_id):
        self.title, self.id, self.values = title, sheet_id, None

    def clear(self):
        self.values = None

    def update(self, values, range_name, value_input_option=None):
        assert value_input_option == "RAW"
        for row in values:
            for v in row:
                assert v is None or isinstance(v, (str, int, float)), (self.title, v)
        self.values = values


class FakeSpreadsheet:
    def __init__(self):
        self.sheets, self.requests = {}, []

    def worksheet(self, name):
        if name not in self.sheets:
            import gspread
            raise gspread.exceptions.WorksheetNotFound(name)
        return self.sheets[name]

    def add_worksheet(self, name, rows, cols):
        self.sheets[name] = FakeWorksheet(name, len(self.sheets) + 1)
        return self.sheets[name]

    def batch_update(self, body):
        self.requests.extend(body["requests"])


class DeadlineTests(unittest.TestCase):

    def entry(self, **kw):
        base = {"type": "Notification", "acquisition_status": "Under assessment",
                "notification_date": "11 September 2026", "end_of_determination_period": "26 Oct 2026"}
        return {**base, **kw}

    def test_progress_and_status(self):
        info = reporting.deadline_info(self.entry(), datetime.date(2026, 10, 12))
        self.assertEqual(info["label"], "Day 20 of 30")
        self.assertEqual(info["remaining"], 10)
        self.assertEqual(info["status"], "Due within 10 business days")
        self.assertEqual(reporting.deadline_info(self.entry(), datetime.date(2026, 10, 26))["status"], "Due today")
        self.assertEqual(reporting.deadline_info(self.entry(), datetime.date(2026, 10, 27))["status"],
                         "Past deadline (awaiting publication)")
        done = self.entry(acquisition_status="Assessment completed", accc_determination="Approved")
        self.assertEqual(reporting.deadline_info(done)["status"], "Decided")

    def test_watchlist(self):
        e = {"title": "Coles - Paydock", "acquirers": "Coles Group Limited (ABN 11 004 089 936)",
             "anzsic_codes": "6419 Other Auxiliary Finance", "consultation_text": ""}
        wl = {"companies": ["Coles", "Cole"], "sectors": [], "anzsic_codes": ["64"], "keywords": []}
        self.assertEqual(reporting.watchlist_matches(e, wl), ["company: Coles", "ANZSIC: 64"])

    def test_describe_change(self):
        self.assertEqual(reporting.describe_change({"field": "end_of_determination_period",
                                                    "old": "26 Oct 2026", "new": "2026-11-09"}),
                         "End of determination period: 26 Oct 2026 → 9 Nov 2026")


class SheetsTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()
        scraper.run(FakeRequestsSession(site_pages()))

    def test_all_tabs_write(self):
        os.environ["GOOGLE_SHEET_ID"] = "x"
        sh = FakeSpreadsheet()
        push_to_sheets.get_client = lambda: type("C", (), {"open_by_key": lambda self, k: sh})()
        push_to_sheets.main()
        self.assertEqual(set(sh.sheets), {"Raw Data (Do not edit)", "Last Updated", "Dashboard", "Active Matters",
                                          "Case History", "Summary", "Monthly Stats"})
        raw = sh.sheets["Raw Data (Do not edit)"].values
        self.assertEqual(raw[0][0], "Acquisition Name")
        self.assertEqual(len(raw), 1 + len(load_json(REGISTER_JSON, {})))
        # dates are serial numbers (real dates), day counts are ints
        header = raw[0]
        row = next(r for r in raw if r[header.index("Case Number")] == "MN-65033")
        self.assertIsInstance(row[header.index("Notification / Application Date")], int)
        self.assertEqual(row[header.index("Determination Period Business Days (ACT)")], 30)
        dash = [r[0] for r in sh.sheets["Dashboard"].values]
        self.assertIn("Extended timelines (active)", dash)
        self.assertIn("CHALLENGER", " ".join(str(c) for r in sh.sheets["Dashboard"].values for c in r).upper())


if __name__ == "__main__":
    unittest.main()
