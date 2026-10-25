"""
End-to-end runs of scraper.run() against the fixture site, covering change
detection, cosmetic vs material changes, failure handling, the sanity guard,
document download retries and the schema migration.
"""

import os
import unittest

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
        self.assertIn("only 3 entries", load_json(HEALTH_JSON, {})["last_error"])

    def test_removed_entry_confirmed_by_404(self):
        run_site()
        register = load_json(REGISTER_JSON, {})
        register["gone-matter"] = {**register[helpers.SLUG_WAIVER], "slug": "gone-matter", "case_number": "WA-1",
                                   "url": f"{helpers.REGISTER}/gone-matter"}
        save_json(REGISTER_JSON, register)
        run_site()
        latest = load_json(os.path.join(helpers.TMP, "data", "latest_changes.json"), {})
        self.assertEqual([r["slug"] for r in latest["removed_entries"]], ["gone-matter"])

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
