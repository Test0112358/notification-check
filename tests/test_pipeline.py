"""
End-to-end runs of scraper.run() against the fixture site, covering change
detection, cosmetic vs material changes, failure handling, the sanity guard,
document download retries and the schema migration.
"""

import datetime
import os
import unittest
from unittest import mock

import helpers
import documents
import scraper
import verify
from accc_common import (
    CHANGELOG_JSON, DOCUMENTS_MANIFEST_JSON, HEALTH_JSON, OUTBOX_DIR, REGISTER_CSV,
    REGISTER_JSON, REGISTER_XLSX, STATUS_CSV, SUMMARY_JSON, load_json, save_json,
)
from helpers import FakeRequestsSession, fixture, site_pages


def run_site(**kwargs):
    http = FakeRequestsSession(site_pages(**kwargs))
    code = scraper.run(http)
    return code, http


class PipelineTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()

    def test_first_run_builds_everything(self):
        code, http = run_site()
        self.assertEqual(code, 0)
        register = load_json(REGISTER_JSON, {})
        self.assertEqual(len(register), 3)
        active = register[helpers.SLUG_ACTIVE]
        self.assertEqual(active["det_period_business_days"], 30)
        self.assertEqual(active["consultation_close_date"], "2026-10-12")
        self.assertEqual(active["schema_version"], 2)
        for path in (REGISTER_CSV, REGISTER_XLSX, SUMMARY_JSON, STATUS_CSV, HEALTH_JSON,
                     os.path.join(OUTBOX_DIR, "email_changes.html"),
                     os.path.join(OUTBOX_DIR, "email_documents.html"),
                     os.path.join(OUTBOX_DIR, "email_digest.html")):
            self.assertTrue(os.path.exists(path), path)
        # Both the .docx questionnaire and the PDF remedy offer are kept
        manifest = load_json(DOCUMENTS_MANIFEST_JSON, {})
        self.assertEqual(sorted(r["status"] for r in manifest.values()), ["downloaded", "downloaded"])
        self.assertTrue(all(r["filename"].startswith("MN-65033 - ") for r in manifest.values()))
        health = load_json(HEALTH_JSON, {})
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertTrue(health["last_success_utc"])
        with open(REGISTER_CSV, encoding="utf-8-sig") as f:
            csv_text = f.read()
        self.assertNotIn(".0,", csv_text)          # day counts are integers, not 45.0
        self.assertIn("Day ", open(os.path.join(OUTBOX_DIR, "email_changes.html")).read() + "Day ")

    def test_material_change_detected_and_classified(self):
        run_site()
        changed = (fixture("detail_active.html")
                   .replace("Phase 1 - initial assessment", "Phase 2 - detailed assessment")
                   .replace("2026-10-02T17:36:09+10:00", "2026-10-21T09:00:00+11:00")
                   .replace("</table>\n</div>",
                            "<tr><td>21 Oct 2026</td><td>ACCC decided notification is subject to Phase 2 review</td></tr>"
                            "</table>\n</div>"))
        listing = fixture("listing_page.html").replace(
            "<dd>Phase 1 - initial assessment</dd>\n        <dt>Effective notification date</dt><dd>11 Sep 2026</dd>",
            "<dd>Phase 2 - detailed assessment</dd>\n        <dt>Effective notification date</dt><dd>11 Sep 2026</dd>")
        code, _ = run_site(active_html=changed, listing_html=listing)
        self.assertEqual(code, 0)
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual(latest["changed_count"], 1)
        summary = latest["changed_entries"][0]["summary"]
        self.assertTrue(any(s.startswith("Stage: Phase 1") for s in summary), summary)
        self.assertTrue(any("New document: ACCC decided notification is subject to Phase 2 review" in s
                            for s in summary), summary)
        alerts = latest["decision_documents"]
        self.assertEqual(alerts[0]["category"], "Moved to Phase 2")
        self.assertTrue(alerts[0]["priority"])
        html = open(os.path.join(OUTBOX_DIR, "email_changes.html"), encoding="utf-8").read()
        self.assertIn("Priority decisions and events", html)

    def test_cosmetic_change_does_not_alert(self):
        run_site()
        touched = fixture("detail_active.html").replace("2026-10-02T17:36:09+10:00", "2026-10-03T08:00:00+10:00")
        code, _ = run_site(active_html=touched)
        self.assertEqual(code, 0)
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual(latest["changed_count"], 0)
        self.assertEqual(latest["cosmetic_only_count"], 1)
        events = [e for e in load_json(CHANGELOG_JSON, []) if e["event"] == "CHANGED"]
        self.assertFalse(events[-1]["material"])

    def test_blocked_site_fails_fast_and_keeps_data(self):
        run_site()
        before = open(REGISTER_JSON, encoding="utf-8").read()
        http = FakeRequestsSession(blocked=True)
        code = scraper.run(http)
        self.assertEqual(code, 2)
        self.assertEqual(len(http.calls), 1)            # no retries against a 403
        self.assertEqual(open(REGISTER_JSON, encoding="utf-8").read(), before)
        health = load_json(HEALTH_JSON, {})
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertIn("403", health["last_error"])
        email = open(os.path.join(OUTBOX_DIR, "email_failure.html"), encoding="utf-8").read()
        self.assertIn("self-hosted", email)
        scraper.run(FakeRequestsSession(blocked=True))
        self.assertEqual(load_json(HEALTH_JSON, {})["consecutive_failures"], 2)
        self.assertTrue(scraper.should_alert_failure(1))
        self.assertTrue(scraper.should_alert_failure(2))
        self.assertFalse(scraper.should_alert_failure(3))

    def test_sanity_guard_refuses_partial_listing(self):
        stored = {f"old-{i}": {"slug": f"old-{i}", "title": f"Old {i}", "case_number": f"MN-{i}",
                               "url": f"{helpers.REGISTER}/old-{i}", "schema_version": 2}
                  for i in range(30)}
        save_json(REGISTER_JSON, stored)
        code, _ = run_site()
        self.assertEqual(code, 3)
        self.assertEqual(len(load_json(REGISTER_JSON, {})), 30)
        self.assertIn("listed only 3 matters", load_json(HEALTH_JSON, {})["last_error"])

    # A maintenance / bot-check page served with HTTP 200 (seen 23 Nov 2026 at 03:24 Canberra time)
    MAINTENANCE_HTML = ("<!DOCTYPE html><html><head><title>Site maintenance | ACCC</title></head>"
                        "<body><h1>We'll be back soon</h1><p>The site is undergoing maintenance.</p>"
                        '<a href="/public-registers/acquisitions-and-mergers-registers/acquisitions-register">'
                        "Acquisitions register</a></body></html>")

    def _store_30(self):
        stored = {f"old-{i}": {"slug": f"old-{i}", "title": f"Old {i}", "case_number": f"MN-{i}",
                               "url": f"{helpers.REGISTER}/old-{i}", "schema_version": 2}
                  for i in range(30)}
        save_json(REGISTER_JSON, stored)
        return open(REGISTER_JSON, encoding="utf-8").read()

    def test_non_register_page_retries_then_fails_as_unavailable(self):
        before = self._store_30()
        listing_url = f"{helpers.REGISTER}?items_per_page=50&page=0"
        http = FakeRequestsSession({listing_url: (200, self.MAINTENANCE_HTML)})
        code = scraper.run(http)
        self.assertEqual(code, 4)
        self.assertEqual(http.calls, [listing_url, listing_url])      # exactly one retry
        self.assertEqual(open(REGISTER_JSON, encoding="utf-8").read(), before)
        health = load_json(HEALTH_JSON, {})
        self.assertEqual(health["last_status"], "failed: unavailable")
        self.assertIn("Site maintenance | ACCC", health["last_error"])
        failed_dir = os.path.join(OUTBOX_DIR, "failed_pages")
        self.assertEqual(sorted(os.listdir(failed_dir)),
                         ["listing-page-1-attempt-1.html", "listing-page-1-attempt-2.html"])
        email = open(os.path.join(OUTBOX_DIR, "email_failure.html"), encoding="utf-8").read()
        self.assertIn("without the register", email)
        self.assertNotIn("parse_card() needs updating", email)
        # No email on the first unavailable run; from the second in a row, about once a day
        self.assertFalse(scraper.should_alert_failure(1, "unavailable"))
        self.assertTrue(scraper.should_alert_failure(2, "unavailable"))
        self.assertFalse(scraper.should_alert_failure(3, "unavailable"))
        self.assertTrue(scraper.should_alert_failure(4, "unavailable"))

    def test_non_register_page_recovers_on_retry(self):
        stored = {f"old-{i}": {"slug": f"old-{i}"} for i in range(30)}
        listing_url = f"{helpers.REGISTER}?items_per_page=50&page=0"
        responses = [self.MAINTENANCE_HTML, fixture("listing_page.html")]

        class FlakySite(FakeRequestsSession):
            def get(self, url, timeout=None, headers=None):
                self.calls.append(url)
                return helpers.FakeResponse(200, responses.pop(0))

        http = FlakySite()
        with mock.patch.object(scraper, "MIN_FIRST_PAGE_CARDS", 3):
            entries, _ = scraper.fetch_listing_entries(
                scraper.PoliteSession(http), stored, full=False, today=datetime.date(2026, 11, 24))
        self.assertEqual(http.calls, [listing_url, listing_url])
        self.assertEqual(sorted(entries), sorted([helpers.SLUG_ACTIVE, helpers.SLUG_COMPLETED, helpers.SLUG_WAIVER]))

    def test_layout_change_still_fails_immediately_without_retry(self):
        # Register page with its pager but cards the parser cannot read: a real layout change
        before = self._store_30()
        broken = fixture("listing_page.html").replace("/acquisitions-register/", "/acquisition-matters/")
        listing_url = f"{helpers.REGISTER}?items_per_page=50&page=0"
        http = FakeRequestsSession({listing_url: (200, broken)})
        code = scraper.run(http)
        self.assertEqual(code, 3)
        self.assertEqual(http.calls, [listing_url])                   # no retry
        self.assertEqual(open(REGISTER_JSON, encoding="utf-8").read(), before)
        self.assertIn("listed only 0 matters", load_json(HEALTH_JSON, {})["last_error"])
        self.assertTrue(scraper.should_alert_failure(1, "sanity"))
        self.assertTrue(os.path.exists(os.path.join(OUTBOX_DIR, "failed_pages", "listing-page-1.html")))

    def test_removed_entry_confirmed_by_404(self):
        run_site()
        register = load_json(REGISTER_JSON, {})
        register["gone-matter"] = {**register[helpers.SLUG_WAIVER], "slug": "gone-matter", "case_number": "WA-1",
                                   "url": f"{helpers.REGISTER}/gone-matter"}
        save_json(REGISTER_JSON, register)
        run_site()
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual([r["slug"] for r in latest["removed_entries"]], ["gone-matter"])

    def test_renamed_matter_keeps_its_history(self):
        # Like MN-25049: the ACCC renamed "merck-kgaa-bio-techne" to "merck-kgaa%E2%80%93bio-techne"
        run_site()
        first_seen = load_json(REGISTER_JSON, {})[helpers.SLUG_ACTIVE]["first_seen_utc"]
        new_slug = "challenger-%E2%80%93-channel-capital-and-fidante"
        listing = fixture("listing_page.html").replace(helpers.SLUG_ACTIVE, new_slug)
        decided = (fixture("detail_active.html")
                   .replace("<h1>Challenger - Channel Capital and Fidante</h1>", "<h1>Challenger – Channel Capital and Fidante</h1>")
                   .replace("<p>Under assessment</p>", "<p>Assessment completed</p>"))
        pages = site_pages(listing_html=listing)
        pages[f"{helpers.REGISTER}/{new_slug}"] = (200, decided)
        self.assertEqual(scraper.run(FakeRequestsSession(pages)), 0)

        register = load_json(REGISTER_JSON, {})
        self.assertNotIn(helpers.SLUG_ACTIVE, register)
        self.assertEqual(register[new_slug]["first_seen_utc"], first_seen)
        self.assertEqual(register[new_slug]["previous_slugs"], [helpers.SLUG_ACTIVE])
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual((latest["new_count"], latest["removed_count"], latest["changed_count"]), (0, 0, 1))
        summary = latest["changed_entries"][0]["summary"]
        self.assertIn("Status: Under assessment → Assessment completed", summary)
        manifest = load_json(DOCUMENTS_MANIFEST_JSON, {})
        self.assertTrue(all(r["slug"] == new_slug for r in manifest.values()))

    def test_old_format_records_migrate_without_alerts(self):
        run_site()
        register = load_json(REGISTER_JSON, {})
        old = register[helpers.SLUG_ACTIVE]
        old["schema_version"] = 1
        old["acquirers"] = "CHALLENGER FUNDS MANAGEMENT HOLDINGS PTY LIMITED; 74 107 728 030"
        old["targets"] = "CHANNEL GROUP HOLDINGS PTY LTD; 699 691 622"     # 'AGL' was dropped by the old parser
        text = old["consultation_text"]
        old["consultation_text"] = "The ACCC has prepared a questionnaire on the Acquisition. " + text
        save_json(REGISTER_JSON, register)
        code, _ = run_site()
        self.assertEqual(code, 0)
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual(latest["changed_count"], 0)
        new = load_json(REGISTER_JSON, {})[helpers.SLUG_ACTIVE]
        self.assertIn("AGL (ACN 004 028 077)", new["targets"])

    def test_verifier_passes_on_scraped_data(self):
        run_site()
        self.assertTrue(verify.run())
        report = load_json(os.path.join(helpers.TMP, "data", "verification_report.json"), [])
        self.assertFalse([i for i in report if i["severity"] == "ERROR"], report)


class DocumentRetryTests(unittest.TestCase):

    def setUp(self):
        helpers.reset_dirs()

    def _register(self):
        return {"m": {"slug": "m", "case_number": "MN-1", "title": "Matter", "url": "u", "consultation_docs": [
            {"date": "1 Oct 2026", "description": "Questionnaire", "url": "https://x/a/Questionnaire.docx"},
            {"date": "1 Oct 2026", "description": "Remedy offer", "url": "https://x/b/Questionnaire.docx"},
        ]}}

    def test_documents_beyond_cap_stay_pending(self):
        from accc_common import PoliteSession
        manifest = {}
        documents.sync_manifest(manifest, self._register(), "t1")
        http = FakeRequestsSession({"https://x/a/Questionnaire.docx": (200, b"one"),
                                    "https://x/b/Questionnaire.docx": (200, b"two")})
        first = documents.download_pending(manifest, PoliteSession(http), "t1", limit=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(documents.manifest_counts(manifest)["pending"], 1)
        second = documents.download_pending(manifest, PoliteSession(http), "t2", limit=1)
        self.assertEqual(len(second), 1)
        names = sorted(r["filename"] for r in manifest.values())
        self.assertEqual(len(set(names)), 2, names)      # same original name, no overwrite

    def test_failed_download_is_retried(self):
        from accc_common import PoliteSession
        manifest = {}
        documents.sync_manifest(manifest, self._register(), "t1")
        broken = FakeRequestsSession({}, default_status=404)
        documents.download_pending(manifest, PoliteSession(broken), "t1")
        self.assertEqual(documents.manifest_counts(manifest)["failed"], 2)
        fixed = FakeRequestsSession({"https://x/a/Questionnaire.docx": (200, b"one"),
                                     "https://x/b/Questionnaire.docx": (200, b"two")})
        documents.download_pending(manifest, PoliteSession(fixed), "t2")
        self.assertEqual(documents.manifest_counts(manifest)["downloaded"], 2)

    def test_backfill_from_changelog_and_existing_files(self):
        changelog = [{"slug": "m", "title": "Matter", "url": "u", "event": "CHANGED", "changes": [{
            "field": "consultation_docs", "old": "",
            "new": "[{'date': '1 Jul 2026', 'description': 'Remedy offer', "
                   "'url': 'https://x/docs/Remedy%20Offer.pdf'}]"}]}]
        save_json(CHANGELOG_JSON, changelog)
        os.makedirs(documents.CONSULTATION_DOCS_DIR, exist_ok=True)
        with open(os.path.join(documents.CONSULTATION_DOCS_DIR, "Questionnaire.docx"), "wb") as f:
            f.write(b"old")
        manifest = {}
        documents.sync_manifest(manifest, self._register(), "t1")
        self.assertEqual(manifest["https://x/docs/Remedy%20Offer.pdf"]["status"], "pending")
        self.assertEqual(manifest["https://x/docs/Remedy%20Offer.pdf"]["source"], "changelog backfill")
        self.assertEqual(manifest["https://x/a/Questionnaire.docx"]["status"], "downloaded")


if __name__ == "__main__":
    unittest.main()
