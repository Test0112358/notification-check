"""
Request budget: the listing stops early on routine runs, case pages follow the
lifecycle re-check schedule, and document downloads put new documents first
and stop at the first refusal.
"""

import datetime
import unittest

import helpers
import documents
import scraper
import verify
from accc_common import PoliteSession, listing_is_complete
from helpers import REGISTER, FakeRequestsSession

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
TODAY = datetime.date(2026, 11, 10)


def card_html(slug, status="Assessment completed", stage="Waiver application", date="1 October 2026"):
    return (f'<a href="/public-registers/acquisitions-and-mergers-registers/acquisitions-register/{slug}">'
            f"<div>{slug}</div><dl><dt>Acquisition status</dt><dd>{status}</dd><dt>Type</dt><dd>Waiver</dd>"
            f"<dt>Case number</dt><dd>WA-{abs(hash(slug)) % 99999}</dd><dt>Stage</dt><dd>{stage}</dd>"
            f"<dt>Waiver application date</dt><dd>{date}</dd></dl></a>")


def listing_site(pages, dates=None):
    """pages: list of lists of slugs → {url: (200, html)}; dates: notification date per page."""
    last = len(pages) - 1
    site = {}
    for i, slugs in enumerate(pages):
        date = dates[i] if dates else "1 October 2026"
        pager = f'<a href="?items_per_page=50&amp;page={last}" title="Go to last page">Last</a>'
        site[f"{REGISTER}?items_per_page=50&page={i}"] = (
            200, "<html><body>" + "".join(card_html(s, date=date) for s in slugs) + f"<nav>{pager}</nav></body></html>")
    return site


def stored(slug, **kw):
    base = {"slug": slug, "title": slug, "url": f"{REGISTER}/{slug}", "type": "Waiver",
            "case_number": f"WA-{abs(hash(slug)) % 99999}", "stage": "Waiver application",
            "acquisition_status": "Assessment completed", "notification_date": "1 October 2026",
            "determination_publication_date": "1 September 2026",
            "last_checked_utc": "2026-11-09T22:00:00Z", "schema_version": 2}
    return {**base, **kw}


class ListingDepthTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()
        self.pages = [[f"m{p}-{i}" for i in range(12)] for p in range(5)]
        self.stored = {s: stored(s) for page in self.pages for s in page}

    def test_routine_run_stops_after_known_pages(self):
        del self.stored["m0-0"]                       # one new matter on page 1
        http = FakeRequestsSession(listing_site(self.pages))
        entries, complete = scraper.fetch_listing_entries(PoliteSession(http), self.stored, False, TODAY)
        self.assertEqual(len(http.calls), 2)          # page 1 + minimum page 2, then stop
        self.assertFalse(complete)
        self.assertFalse(listing_is_complete())
        self.assertIn("m0-0", entries)

    def test_keeps_paging_through_the_last_30_days(self):
        dates = ["5 November 2026", "25 October 2026", "15 October 2026", "1 October 2026", "1 September 2026"]
        http = FakeRequestsSession(listing_site(self.pages, dates))
        scraper.fetch_listing_entries(PoliteSession(http), self.stored, False, TODAY)
        self.assertEqual(len(http.calls), 4)          # page 3 still reaches into the last 30 days

    def test_full_scan_fetches_every_page(self):
        http = FakeRequestsSession(listing_site(self.pages))
        entries, complete = scraper.fetch_listing_entries(PoliteSession(http), self.stored, True, TODAY)
        self.assertEqual(len(http.calls), 5)
        self.assertTrue(complete)
        self.assertTrue(listing_is_complete())
        self.assertEqual(len(entries), 60)


class RecheckScheduleTests(unittest.TestCase):

    def reason(self, **kw):
        return scraper.recheck_reason(stored("x", **kw), NOW, TODAY)

    def test_active_matters(self):
        active = dict(acquisition_status="Under assessment", type="Notification", determination_publication_date="",
                      notification_date="1 October 2026", end_of_determination_period="20 Nov 2026")
        self.assertIsNone(self.reason(**active))                                   # checked 2h ago
        self.assertEqual(self.reason(**active, last_checked_utc="2026-11-09T00:00:00Z"), "under assessment (daily)")
        due = {**active, "end_of_determination_period": "11 Nov 2026"}
        self.assertEqual(self.reason(**due), "decision or consultation due")       # every run
        closing = {**active, "consultation_close_date": "2026-11-11"}
        self.assertEqual(self.reason(**closing), "decision or consultation due")

    def test_decided_matters(self):
        self.assertIsNone(self.reason(determination_publication_date="6 November 2026"))
        self.assertEqual(self.reason(determination_publication_date="6 November 2026",
                                     last_checked_utc="2026-11-08T00:00:00Z"), "decided this week (daily)")
        self.assertIsNone(self.reason(determination_publication_date="25 October 2026",
                                      last_checked_utc="2026-11-08T00:00:00Z"))
        self.assertEqual(self.reason(determination_publication_date="25 October 2026",
                                     last_checked_utc="2026-11-01T00:00:00Z"), "decided recently (weekly)")
        self.assertIsNone(self.reason(determination_publication_date="1 August 2026",
                                      last_checked_utc="2026-06-01T00:00:00Z"))   # cold: sweep only

    def test_slow_sweep_is_small_and_oldest_first(self):
        register = {f"c{i}": stored(f"c{i}", last_checked_utc=f"2026-10-{10 + i % 20:02d}T00:00:00Z")
                    for i in range(800)}
        chosen = scraper.select_detail_fetches({}, register, now=NOW, today=TODAY)
        self.assertEqual(set(chosen.values()), {"slow sweep"})
        self.assertEqual(len(chosen), 5)              # 800 pages over ~90 days of 15 runs a week
        self.assertTrue(all(register[s]["last_checked_utc"].startswith("2026-10-10") for s in chosen))


class DocumentQueueTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()

    def test_new_documents_first_and_stop_on_403(self):
        manifest = {}
        old = {"slug": "o", "case_number": "MN-1", "title": "Old"}
        new = {"slug": "n", "case_number": "MN-2", "title": "New"}
        for i in range(3):
            documents.register_document(manifest, f"https://x/old{i}.pdf", old, {"description": "Remedy offer"},
                                        "changelog backfill")
        documents.register_document(manifest, "https://x/new.pdf", new, {"description": "Remedy offer"}, "register")
        queue = documents.pending_documents(manifest)
        self.assertEqual([r["url"] for r in queue], ["https://x/new.pdf", "https://x/old0.pdf"])
        http = FakeRequestsSession({"https://x/new.pdf": (403, "denied")})
        documents.download_pending(manifest, PoliteSession(http), "t1")
        self.assertEqual(http.calls, ["https://x/new.pdf"])   # stopped after the first 403
        self.assertEqual(manifest["https://x/new.pdf"]["status"], "failed")


class VerifierTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()

    def test_partial_listing_does_not_flag_absent_matters(self):
        from accc_common import cache_listing_html
        cache_listing_html(0, listing_site([["a"]])[f"{REGISTER}?items_per_page=50&page=0"][1])
        issues = verify.layer2_cross_source({"a": stored("a"), "b": stored("b")})
        self.assertFalse([i for i in issues if i["rule"] == "ENTRY_STORED_BUT_NOT_IN_LISTING"])

    def test_public_benefit_stage_casing(self):
        issues = verify.layer1_consistency({"a": stored("a", type="Notification", stage="Public benefit phase",
                                                        case_number="MN-1")})
        self.assertFalse([i for i in issues if i["rule"] == "UNKNOWN_STAGE_VALUE"])


if __name__ == "__main__":
    unittest.main()
