"""Equivalence + edge tests for shared/trading_calendar (Phase 2 step 1).

The suite pins the EXACT current behavior of every call site it replaces:
- learning semantics: horizon 0 = start day (at_least_one=False)
- report semantics: horizon 0 = next weekday (at_least_one=True)
- last-weekday-of-month incl. December and Saturday/Sunday month-ends
- next-month-last-weekday str wrapper used by report.py
- last_n_weekdays walk (macro)
- count_distinct_dates_since SQL helper (fund nav / stock prices)
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from shared import trading_calendar as tc


class NextWeekdayDateTest(unittest.TestCase):
    def test_learning_semantics_zero_is_same_day(self) -> None:
        # learning.py 历史行为：horizon 0 停在起始日
        self.assertEqual(tc.next_weekday_date("2026-10-28", 0), "2026-10-28")
        self.assertEqual(tc.next_weekday_date("2026-10-28", -3), "2026-10-28")

    def test_report_semantics_zero_is_next_weekday(self) -> None:
        # report.py 历史行为：max(1, trading_days)
        self.assertEqual(
            tc.next_weekday_date("2026-10-28", 0, at_least_one=True), "2026-10-29"
        )

    def test_friday_plus_one_is_monday(self) -> None:
        self.assertEqual(tc.next_weekday_date("2026-10-30", 1), "2026-11-02")
        self.assertEqual(
            tc.next_weekday_date("2026-10-30", 1, at_least_one=True), "2026-11-02"
        )

    def test_two_and_five_skipping_weekends(self) -> None:
        # Wed 2026-10-28 +2 weekdays → Fri 10-30；+5 → Wed 11-04
        self.assertEqual(tc.next_weekday_date("2026-10-28", 2), "2026-10-30")
        self.assertEqual(tc.next_weekday_date("2026-10-28", 5), "2026-11-04")

    def test_call_site_wrappers_keep_their_semantics(self) -> None:
        from fund_tracker.learning import next_weekday_date as learning_version
        from fund_tracker.report import next_weekday_date as report_version

        for horizon, expected_learning, expected_report in (
            (0, "2026-10-28", "2026-10-29"),
            (1, "2026-10-29", "2026-10-29"),
            (2, "2026-10-30", "2026-10-30"),
            (5, "2026-11-04", "2026-11-04"),
        ):
            self.assertEqual(learning_version("2026-10-28", horizon), expected_learning)
            self.assertEqual(report_version("2026-10-28", horizon), expected_report)


class LastWeekdayOfMonthTest(unittest.TestCase):
    def test_known_months(self) -> None:
        self.assertEqual(tc.last_weekday_of_month(date(2026, 10, 5)), date(2026, 10, 30))
        self.assertEqual(tc.last_weekday_of_month(date(2026, 11, 1)), date(2026, 11, 30))
        self.assertEqual(tc.last_weekday_of_month(date(2026, 5, 14)), date(2026, 5, 29))

    def test_saturday_month_end(self) -> None:
        self.assertEqual(tc.last_weekday_of_month(date(2026, 10, 31)), date(2026, 10, 30))

    def test_sunday_month_end(self) -> None:
        # 2027-02-28 is a Sunday → last weekday Friday 02-26.
        self.assertEqual(tc.last_weekday_of_month(date(2027, 2, 27)), date(2027, 2, 26))

    def test_december(self) -> None:
        self.assertEqual(tc.last_weekday_of_month(date(2026, 12, 3)), date(2026, 12, 31))
        self.assertEqual(tc.last_weekday_of_month(date(2027, 12, 31)), date(2027, 12, 31))
        # 2024-12-31 is a Tuesday; 2023-12-31 is a Sunday → Friday 12-29
        self.assertEqual(tc.last_weekday_of_month(date(2023, 12, 15)), date(2023, 12, 29))

    def test_is_last_weekday(self) -> None:
        self.assertTrue(tc.is_last_weekday(date(2026, 10, 30)))
        self.assertFalse(tc.is_last_weekday(date(2026, 10, 29)))
        self.assertFalse(tc.is_last_weekday(date(2026, 10, 31)))  # weekend itself

    def test_is_weekend(self) -> None:
        self.assertTrue(tc.is_weekend(date(2026, 10, 31)))  # Saturday
        self.assertTrue(tc.is_weekend(date(2026, 11, 1)))  # Sunday
        self.assertFalse(tc.is_weekend(date(2026, 10, 30)))  # Friday

    def test_model_review_aliases_match(self) -> None:
        from fund_tracker.model_review import (
            is_last_weekday as review_is,
            last_weekday_of_month as review_last,
        )

        self.assertIs(review_is, tc.is_last_weekday)
        self.assertIs(review_last, tc.last_weekday_of_month)

    def test_cli_utils_alias_matches(self) -> None:
        from shared.cli_utils import _last_weekday_of_month

        self.assertIs(_last_weekday_of_month, tc.last_weekday_of_month)


class NextMonthLastWeekdayTest(unittest.TestCase):
    def test_from_october_to_last_weekday_of_november(self) -> None:
        self.assertEqual(tc.next_month_last_weekday(date(2026, 10, 1)), date(2026, 11, 30))

    def test_report_wrapper_keeps_isostring_io(self) -> None:
        from fund_tracker.report import next_month_last_weekday as report_version

        self.assertEqual(report_version("2026-10-01"), "2026-11-30")

    def test_december_wraps_to_next_january(self) -> None:
        # 2027-01-31 is a Sunday → Friday 01-29.
        self.assertEqual(tc.next_month_last_weekday(date(2026, 12, 5)), date(2027, 1, 29))

    def test_same_month_independence(self) -> None:
        for day in (1, 15, 28):
            self.assertEqual(
                tc.next_month_last_weekday(date(2026, 10, day)), date(2026, 11, 30)
            )


class LastNWeekdaysTest(unittest.TestCase):
    def test_walk_back_three(self) -> None:
        # Wed → [Mon, Tue, Wed]
        self.assertEqual(
            tc.last_n_weekdays(3, today=date(2026, 10, 28)),
            [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)],
        )

    def test_weekend_today_excluded(self) -> None:
        # Sunday 2026-11-01 → [Wed, Thu, Fri] of the prior week
        self.assertEqual(
            tc.last_n_weekdays(3, today=date(2026, 11, 1)),
            [date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)],
        )

    def test_macro_wrapper_matches(self) -> None:
        from fund_tracker.macro import _last_weekdays

        self.assertEqual(_last_weekdays(5), tc.last_n_weekdays(5))

    def test_zero_count_empty(self) -> None:
        self.assertEqual(tc.last_n_weekdays(0, today=date(2026, 10, 28)), [])


class CountsDistinctDatesTest(unittest.TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE nav (code TEXT, date TEXT, unit_nav REAL)")
        rows = [
            ("000001", "2026-10-23", 1.0),
            ("000001", "2026-10-26", 1.1),
            ("000001", "2026-10-27", 1.2),
            ("000001", "2026-10-27", 1.2),  # duplicate row, one distinct date
        ]
        connection.executemany("INSERT INTO nav VALUES (?, ?, ?)", rows)
        connection.commit()
        self.connection = connection

    def tearDown(self) -> None:
        self.connection.close()

    def test_open_left_closed_right_interval(self) -> None:
        self.assertEqual(
            tc.count_distinct_dates_since(self.connection, "nav", "2026-10-23", "2026-10-27"), 2
        )

    def test_start_equals_end_is_zero(self) -> None:
        self.assertEqual(
            tc.count_distinct_dates_since(self.connection, "nav", "2026-10-27", "2026-10-27"), 0
        )

    def test_duplicates_count_once(self) -> None:
        # (10-26, 10-28] 含 10-27（重复行出现两次），distinct 只算 1
        self.assertEqual(
            tc.count_distinct_dates_since(self.connection, "nav", "2026-10-26", "2026-10-28"), 1
        )

    def test_whitelist_rejects_unknown_table(self) -> None:
        with self.assertRaises(ValueError):
            tc.count_distinct_dates_since(self.connection, "users", "2026-01-01", "2026-12-31")

    def test_store_methods_delegate(self) -> None:
        """Round-trips through the actual store methods (temp DB file)."""
        with tempfile.TemporaryDirectory() as tmp:
            from fund_tracker.store import Database

            database = Database(Path(tmp) / "fund.db")
            with database._connect() as connection:
                connection.executescript("CREATE TABLE nav (code TEXT, date TEXT, unit_nav REAL)")
                connection.executemany(
                    "INSERT INTO nav VALUES (?, ?, ?)",
                    [("000001", "2026-10-26", 1.0), ("000001", "2026-10-27", 1.1)],
                )
                connection.commit()
            self.assertEqual(
                database.count_trading_days_since("2026-10-26", "2026-10-27"), 1
            )


if __name__ == "__main__":
    unittest.main()
