from __future__ import annotations

import unittest
from datetime import date

from fund_tracker.freshness import assess, banner

TODAY = date(2026, 10, 29)


def rows_with(*dates: str) -> list[dict]:
    return [{"code": f"{i:06d}", "nav_date": value} for i, value in enumerate(dates)]


class FreshnessTest(unittest.TestCase):
    def test_fresh_data_is_ok(self) -> None:
        quality = assess(rows_with("2026-10-28", "2026-10-28"), {}, today=TODAY)
        self.assertEqual(quality["level"], "ok")
        self.assertEqual(banner(quality), "")

    def test_one_stale_row_is_warn_not_stale(self) -> None:
        quality = assess(
            rows_with("2026-10-28", "2026-10-28", "2026-10-20"), {}, today=TODAY
        )
        self.assertEqual(quality["level"], "warn")
        self.assertEqual(banner(quality), "")

    def test_mostly_stale_rows_are_stale(self) -> None:
        quality = assess(
            rows_with(
                "2026-10-19",
                "2026-10-19",
                "2026-10-19",
                "2026-10-19",
                "2026-10-28",
            ),
            {},
            today=TODAY,
        )
        self.assertEqual(quality["level"], "stale")
        self.assertIn("不推送正常结论", banner(quality))

    def test_missing_nav_dates_count_as_hard_missing(self) -> None:
        quality = assess([{"code": "000001"}, {"code": "000002"}], {}, today=TODAY)
        self.assertEqual(quality["level"], "stale")
        self.assertEqual(quality["missing"], 2)

    def test_stale_holdings_raise_warn(self) -> None:
        quality = assess(
            rows_with("2026-10-28"),
            {"loaded": True, "as_of": "2026-10-10"},
            today=TODAY,
        )
        self.assertEqual(quality["level"], "warn")
        self.assertTrue(any("持仓快照" in reason for reason in quality["reasons"]))

    def test_custom_threshold_via_config(self) -> None:
        config = {"freshness": {"warn_days": 1, "hard_days": 2, "hard_ratio": 0.5}}
        quality = assess(rows_with("2026-10-24", "2026-10-28"), {}, config, today=TODAY)
        self.assertEqual(quality["level"], "stale")
        self.assertEqual(quality["hard_days"], 2)


if __name__ == "__main__":
    unittest.main()
