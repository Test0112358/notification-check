import datetime
import random
import unittest

import helpers  # noqa: F401  (sets up paths before project imports)
from accc_common import calc_period_days, count_accc_business_days, fmt_canberra, parse_date
from verify import recalculate_business_days

D = datetime.date


class BusinessDayTests(unittest.TestCase):

    def test_phase1_is_30_business_days_with_day0_convention(self):
        # Real matter MN-65033: notified 11 Sep 2026, period ends 26 Oct 2026.
        # The old inclusive count reported 31.
        self.assertEqual(count_accc_business_days(D(2026, 9, 11), D(2026, 10, 26)), 30)
        self.assertEqual(calc_period_days("11 September 2026", "26 Oct 2026"), (45, 30))

    def test_start_day_not_counted(self):
        # Mon → Tue is one business day
        self.assertEqual(count_accc_business_days(D(2026, 10, 19), D(2026, 10, 20)), 1)
        self.assertEqual(count_accc_business_days(D(2026, 10, 19), D(2026, 10, 19)), 0)

    def test_holidays_and_weekends_excluded(self):
        # Labour Day (5 Oct 2026) falls inside Fri 2 Oct → Tue 6 Oct
        self.assertEqual(count_accc_business_days(D(2026, 10, 2), D(2026, 10, 6)), 1)

    def test_christmas_suspension_excluded(self):
        # 22 Dec 2026 → 12 Jan 2027: only 12 Jan counts
        self.assertEqual(count_accc_business_days(D(2026, 12, 22), D(2027, 1, 12)), 1)

    def test_verifier_uses_same_convention_with_different_algorithm(self):
        rng = random.Random(7)
        for _ in range(300):
            start = D(2025, 7, 1) + datetime.timedelta(days=rng.randint(0, 1200))
            end = start + datetime.timedelta(days=rng.randint(0, 200))
            self.assertEqual(
                count_accc_business_days(start, end),
                recalculate_business_days(start.isoformat(), end.isoformat()),
                f"{start} → {end}",
            )

    def test_invalid_ranges(self):
        self.assertIsNone(count_accc_business_days(D(2026, 5, 2), D(2026, 5, 1)))
        self.assertEqual(calc_period_days("", "1 May 2026"), (None, None))


class DateAndTimezoneTests(unittest.TestCase):

    def test_parse_date_formats(self):
        for text in ("14 May 2026", "14 May 2026", "May 14, 2026", "2026-05-14", "14/05/2026"):
            self.assertEqual(parse_date(text), D(2026, 5, 14), text)
        self.assertEqual(parse_date("3 Sept 2026"), D(2026, 9, 3))

    def test_canberra_time_uses_daylight_saving(self):
        self.assertEqual(fmt_canberra("2026-10-25T00:00:00Z"), "25 Oct 2026 11:00 AEDT")
        self.assertEqual(fmt_canberra("2026-07-01T00:00:00Z"), "01 Jul 2026 10:00 AEST")


if __name__ == "__main__":
    unittest.main()
