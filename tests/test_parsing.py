import unittest

from bs4 import BeautifulSoup

import helpers
import documents
import scraper
from helpers import fixture
from verify import parse_listing_for_verification, re_parse_detail_independently


class ListingTests(unittest.TestCase):

    def test_cards(self):
        entries = {}
        scraper.parse_listing_page(BeautifulSoup(fixture("listing_page.html"), "lxml"), entries)
        self.assertEqual(set(entries), {helpers.SLUG_ACTIVE, helpers.SLUG_COMPLETED, helpers.SLUG_WAIVER})
        card = entries[helpers.SLUG_ACTIVE]
        self.assertEqual(card["title"], "Challenger - Channel Capital and Fidante")
        self.assertEqual(card["acquisition_status"], "Under assessment")
        self.assertEqual(card["case_number"], "MN-65033")
        self.assertEqual(card["notification_date"], "11 Sep 2026")
        self.assertEqual(entries[helpers.SLUG_WAIVER]["notification_date"], "22 September 2026")

    def test_verifier_listing_parser_agrees(self):
        soup = BeautifulSoup(fixture("listing_page.html"), "lxml")
        entries = {}
        scraper.parse_listing_page(soup, entries)
        independent = parse_listing_for_verification(soup)
        for slug, card in entries.items():
            for field in ("acquisition_status", "type", "case_number", "stage"):
                self.assertEqual(independent[slug][field], card[field], (slug, field))

    def test_last_page_ignores_items_per_page(self):
        soup = BeautifulSoup('<a href="?items_per_page=50&amp;page=14" title="Go to last page">Last</a>', "lxml")
        self.assertEqual(scraper.get_last_page_index(soup), 14)

    def test_date_format_difference_is_not_a_listing_change(self):
        self.assertFalse(scraper.listing_value_differs("notification_date", "11 Sep 2026", "11 September 2026"))
        self.assertTrue(scraper.listing_value_differs("notification_date", "12 Sep 2026", "11 September 2026"))
        self.assertTrue(scraper.listing_value_differs("stage", "Phase 2 - detailed assessment",
                                                      "Phase 1 - initial assessment"))


class DetailPageTests(unittest.TestCase):

    def setUp(self):
        self.data = scraper.parse_detail_page(BeautifulSoup(fixture("detail_active.html"), "lxml"), "u")

    def test_scalar_fields(self):
        d = self.data
        self.assertEqual(d["title"], "Challenger - Channel Capital and Fidante")
        self.assertEqual(d["acquisition_status"], "Under assessment")
        self.assertEqual(d["case_number"], "MN-65033")
        self.assertEqual(d["type"], "Notification")
        self.assertEqual(d["notification_date"], "11 September 2026")
        self.assertEqual(d["end_of_determination_period"], "26 Oct 2026")
        self.assertEqual(d["last_modified_accc"], "2026-10-02T17:36:09+10:00")
        self.assertTrue(d["anzsic_codes"].startswith("6419"))

    def test_parties_pair_identifiers_and_keep_short_names(self):
        self.assertEqual(self.data["acquirers"],
                         "CHALLENGER FUNDS MANAGEMENT HOLDINGS PTY LIMITED (ABN 74 107 728 030)")
        self.assertEqual(self.data["targets"],
                         "CHANNEL GROUP HOLDINGS PTY LTD (ACN 699 691 622); AGL (ACN 004 028 077)")
        self.assertEqual(self.data["other_parties"],
                         "CHALLENGER LIMITED (ABN 85 106 842 371); Stripe, LLC (Delaware File Number - 4675506)")
        self.assertEqual(scraper.party_names(self.data["targets"]), ["CHANNEL GROUP HOLDINGS PTY LTD", "AGL"])

    def test_consultation_text_is_not_duplicated(self):
        text = self.data["consultation_text"]
        self.assertEqual(text.count("The ACCC has prepared a questionnaire"), 1)
        self.assertNotIn("read more", text)
        self.assertEqual(self.data["consultation_close_date"], "2026-10-12")

    def test_document_tables(self):
        self.assertEqual(len(self.data["consultation_docs"]), 2)
        self.assertTrue(self.data["consultation_docs"][0]["url"].endswith("Questionnaire.docx"))
        self.assertEqual(self.data["decisions_docs"][0]["description"],
                         "Timeline extended by 10 business days – following request by parties")

    def test_independent_parser_agrees(self):
        soup = BeautifulSoup(fixture("detail_active.html"), "lxml")
        fresh = re_parse_detail_independently(soup, "u")
        for field in ("acquisition_status", "case_number", "type", "stage", "notification_date",
                      "end_of_determination_period"):
            self.assertEqual(fresh[field], self.data[field], field)

    def test_completed_and_waiver(self):
        done = scraper.parse_detail_page(BeautifulSoup(fixture("detail_completed.html"), "lxml"), "u")
        self.assertEqual(done["accc_determination"], "Approved with conditions")
        self.assertEqual(done["determination_publication_date"], "16 October 2026")
        waiver = scraper.parse_detail_page(BeautifulSoup(fixture("detail_waiver.html"), "lxml"), "u")
        self.assertEqual(waiver["notification_date"], "22 September 2026")
        self.assertEqual(waiver["targets"], "Myriad360, LLC (US Tax ID: 81-0586437)")


class IdentifierTests(unittest.TestCase):

    def test_identifier_lines(self):
        for line in ("695 902 955", "ABN - 11 164 311 997", "US Tax ID: 81-0586437",
                     "Netherlands registration number (KVK): 34249320", "Company number (Delaware): 4977487",
                     "Spain A64622970", "7016672 - Delaware", "(SE) Reg. no 559180-8646"):
            self.assertTrue(scraper.is_identifier_line(line), line)

    def test_names_are_not_identifiers(self):
        for line in ("Blackbird FOF25 Pty Ltd as trustee for Blackbird Ventures 2025 Follow-On Fund Trust",
                     "ELC No.2 Operations Pty Ltd", "3M Company", "7-Eleven Stores Pty Ltd", "Uber", "BHP"):
            self.assertFalse(scraper.is_identifier_line(line), line)

    def test_page_furniture_ignored(self):
        self.assertEqual(scraper.pair_party_lines(["Uber Technologies Inc", "EIN 45-2647441", "Share", "•"]),
                         ["Uber Technologies Inc (EIN 45-2647441)"])


class ClassificationTests(unittest.TestCase):

    def test_decision_documents(self):
        cases = {
            "Summary of Notice of Competition Concerns": ("Notice of Competition Concerns", True),
            "ACCC decided notification is subject to Phase 2 review": ("Moved to Phase 2", True),
            "Timeline extended by 10 business days – following request by parties": ("Timeline extended", False),
            "Notification waiver determination published": ("Waiver determination", False),
            "Coles - Phase 1 determination": ("Phase 1 determination", False),
            "X - Phase 1 determination with conditions": ("Determination: approved with conditions", True),
            "ACCC accepted s87B undertaking": ("s87B undertaking", True),
        }
        for text, expected in cases.items():
            self.assertEqual(documents.classify_decision_document(text), expected, text)
        self.assertEqual(documents.timeline_extension_days("Timeline extended by 15 business days"), 15)

    def test_consultation_close_dates(self):
        p = documents.parse_consultation_close_date
        self.assertEqual(p("Submissions ... should be provided by 9 October 2026 via email"), "2026-10-09")
        self.assertEqual(p("The ACCC is inviting submissions from interested parties on the Acquisitions "
                           "by 16 June 2026. This request is made"), "2026-06-16")
        self.assertEqual(p("The period for providing submissions or information in response to the "
                           "ACCC’s questionnaire has concluded."), "")
        self.assertEqual(p("On 22 April 2026, Ampol made a further remedy offer"), "")


if __name__ == "__main__":
    unittest.main()
