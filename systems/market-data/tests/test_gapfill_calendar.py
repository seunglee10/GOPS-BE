import unittest
from datetime import time

from alfaka.backfill.gapfill import TradingCalendar, detect_gapfill_ranges


class GapfillCalendarTests(unittest.TestCase):
    def test_gapfill_ranges_include_pre_and_after_market_minutes(self):
        pre_market_ranges = detect_gapfill_ranges(
            "2026-06-25T08:00:00.000Z",
            "2026-06-25T08:03:00.000Z",
            "1m",
            actual_timestamps=[
                "2026-06-25T08:00:00.000Z",
                "2026-06-25T08:02:00.000Z",
            ],
        )
        after_market_ranges = detect_gapfill_ranges(
            "2026-06-25T21:00:00.000Z",
            "2026-06-25T21:03:00.000Z",
            "1m",
            actual_timestamps=[
                "2026-06-25T21:00:00.000Z",
                "2026-06-25T21:02:00.000Z",
            ],
        )

        self.assertEqual(len(pre_market_ranges), 1)
        self.assertEqual(pre_market_ranges[0].start, "2026-06-25T08:01:00.000Z")
        self.assertEqual(pre_market_ranges[0].end, "2026-06-25T08:02:00.000Z")
        self.assertEqual(pre_market_ranges[0].missingCount, 1)
        self.assertEqual(len(after_market_ranges), 1)
        self.assertEqual(after_market_ranges[0].start, "2026-06-25T21:01:00.000Z")
        self.assertEqual(after_market_ranges[0].end, "2026-06-25T21:02:00.000Z")
        self.assertEqual(after_market_ranges[0].missingCount, 1)

    def test_gapfill_ranges_include_overnight_minutes(self):
        ranges = detect_gapfill_ranges(
            "2026-07-02T00:00:00.000Z",
            "2026-07-02T00:03:00.000Z",
            "1m",
            actual_timestamps=[
                "2026-07-02T00:00:00.000Z",
                "2026-07-02T00:02:00.000Z",
            ],
        )

        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start, "2026-07-02T00:01:00.000Z")
        self.assertEqual(ranges[0].end, "2026-07-02T00:02:00.000Z")
        self.assertEqual(ranges[0].missingCount, 1)

    def test_gapfill_ranges_honor_early_close_as_day_session_close(self):
        calendar = TradingCalendar(early_closes={"2026-11-27": time(13, 0)})

        ranges = detect_gapfill_ranges(
            "2026-11-27T18:00:00.000Z",
            "2026-11-27T19:00:00.000Z",
            "1m",
            actual_timestamps=[],
            calendar=calendar,
        )

        self.assertEqual(ranges, [])


if __name__ == "__main__":
    unittest.main()
