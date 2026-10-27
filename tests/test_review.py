from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fund_tracker.cli import _review_due_info
from fund_tracker.report import (
    _buy_range,
    _review_action,
    _sell_range,
    next_weekday_date,
    summary_text_review,
)
from fund_tracker.store import Database


class ReviewRulesTest(unittest.TestCase):
    def test_next_weekday_date_skips_weekend(self) -> None:
        self.assertEqual(next_weekday_date("2026-10-23", 2), "2026-10-27")
        self.assertEqual(next_weekday_date("2026-10-26", 2), "2026-10-28")

    def test_review_is_due_after_two_trading_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            for index, day in enumerate(
                [date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
            ):
                nav = 1.0 + index * 0.001
                database.save_nav(
                    "000001",
                    [(day, nav, 0.0)],
                    [(day, nav)],
                )
            database.save_review(
                "2026-10-23",
                "2026-10-21",
                "2026-10-23",
                "test",
                "review.md",
            )
            config = {"review": {"interval_trading_days": 2}}

            due, _, days, interval, _ = _review_due_info(
                database,
                "2026-10-26",
                config,
            )
            self.assertFalse(due)
            self.assertEqual(days, 1)
            self.assertEqual(interval, 2)

            due, _, days, _, _ = _review_due_info(
                database,
                "2026-10-27",
                config,
            )
            self.assertTrue(due)
            self.assertEqual(days, 2)

    def test_two_day_summary_label(self) -> None:
        rows = [
            {"name": "A", "score": 94.0, "target_weight": 0.2, "ret_window": 0.1},
            {"name": "B", "score": 70.0, "target_weight": 0.2, "ret_window": 0.0},
        ]
        text = summary_text_review(
            "2026-10-23",
            rows,
            Path("review.md"),
            alerts=[],
            window_days=2,
        )
        self.assertIn("2日推荐 2026-10-23", text)
        self.assertIn("组合2日", text)

    def test_bond_buy_range_near_nav(self) -> None:
        row = {
            "category": "短债",
            "nav": 1.2594,
            "target_weight": 0.2,
        }
        value = _buy_range(row)
        self.assertIn("1.2518", value)
        self.assertIn("1.2594", value)

    def test_equity_buy_range_uses_unit_nav_range(self) -> None:
        row = {
            "category": "红利",
            "nav": 1.05,
            "target_weight": 0.1,
            "range_low_1y": 0.90,
            "range_high_1y": 1.20,
        }
        value = _buy_range(row)
        self.assertIn("0.9750", value)
        self.assertIn("1.0500", value)

    def test_equity_sell_range(self) -> None:
        row = {
            "category": "红利",
            "nav": 1.18,
            "target_weight": 0.1,
            "range_low_1y": 0.90,
            "range_high_1y": 1.20,
        }
        value = _sell_range(row)
        self.assertIn("1.1700", value)
        self.assertIn("1.2000", value)
        self.assertIn("止盈区", value)

    def test_action_high_score(self) -> None:
        row = {"score": 94.4, "target_weight": 0.2, "ret_5d": 0.1}
        action, _ = _review_action(row, [])
        self.assertEqual(action, "分批买入/持有")

    def test_action_low_score(self) -> None:
        row = {"score": 70.0, "target_weight": 0.2, "ret_5d": 0.1}
        action, _ = _review_action(row, [])
        self.assertEqual(action, "观察")

    def test_action_manager_change(self) -> None:
        row = {"score": 94.4, "target_weight": 0.2, "ret_5d": 0.1}
        action, _ = _review_action(
            row,
            [{"level": "high", "message": "基金经理变更：张三 → 李四"}],
        )
        self.assertEqual(action, "减仓/复核")

    def test_action_observation(self) -> None:
        row = {"score": 90.0, "target_weight": 0, "ret_5d": -3.0}
        action, _ = _review_action(row, [])
        self.assertEqual(action, "小仓试探")

    def test_summary_counts_actions(self) -> None:
        rows = [
            {"name": "A", "score": 94.0, "target_weight": 0.2, "ret_5d": 0.1},
            {"name": "B", "score": 70.0, "target_weight": 0.2, "ret_5d": 0.0},
        ]
        text = summary_text_review("2026-10-23", rows, Path("review.md"), alerts=[])
        self.assertIn("可以跟进持有 1 只", text)
        self.assertIn("可以调整 1 只", text)

    def test_summary_uses_evidence_for_held_fund(self) -> None:
        rows = [
            {
                "code": "006839",
                "name": "安信聚利增强债券A",
                "score": 87.5,
                "target_weight": 0.1,
                "ret_5d": 0.08,
            }
        ]
        evidence = {
            "006839": {
                "score": 62.9,
                "grade": "C",
                "label": "证据一般",
            }
        }
        text = summary_text_review(
            "2026-10-23",
            rows,
            Path("review.md"),
            alerts=[],
            evidence=evidence,
        )
        self.assertIn("可以调整 1 只", text)
        self.assertIn("可以跟进持有 0 只", text)


if __name__ == "__main__":
    unittest.main()
