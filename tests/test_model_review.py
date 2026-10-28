from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fund_tracker.learning import record_predictions
from fund_tracker.model_review import (
    _group_for,
    _stats,
    build_report,
    is_last_weekday,
    last_weekday_of_month,
)
from fund_tracker.store import Database as FundDatabase
from stock_tracker.store import Database as StockDatabase


class ModelReviewDateTest(unittest.TestCase):
    def test_last_weekday_of_month(self) -> None:
        self.assertEqual(
            last_weekday_of_month(date(2026, 10, 1)).isoformat(),
            "2026-10-30",
        )
        self.assertEqual(
            last_weekday_of_month(date(2026, 11, 1)).isoformat(),
            "2026-11-30",
        )
        self.assertEqual(
            last_weekday_of_month(date(2026, 5, 1)).isoformat(),
            "2026-05-29",
        )
        self.assertTrue(is_last_weekday(date(2026, 10, 30)))
        self.assertFalse(is_last_weekday(date(2026, 10, 29)))


class ModelReviewStatsTest(unittest.TestCase):
    def test_stats_separates_directional_and_neutral(self) -> None:
        rows = [
            {"outcome_status": "hit", "outcome_return": 2.0, "max_adverse": -0.5},
            {"outcome_status": "miss", "outcome_return": -1.0, "max_adverse": -2.0},
            {"outcome_status": "neutral", "outcome_return": 0.2, "max_adverse": -1.0},
            {"outcome_status": "pending"},
        ]
        stats = _stats(rows)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["evaluated"], 3)
        self.assertEqual(stats["directional"], 2)
        self.assertEqual(stats["hit"], 1)
        self.assertEqual(stats["hit_rate"], 50.0)
        self.assertAlmostEqual(stats["avg_return"], 0.4)
        self.assertAlmostEqual(stats["avg_adverse"], -1.166666, places=3)
        self.assertEqual(stats["pending"], 1)

    def test_group_fallback(self) -> None:
        self.assertEqual(_group_for({"group_name": "可以出货"}), "可以出货")
        self.assertEqual(_group_for({"action": "再平衡"}), "可以调整")
        self.assertEqual(_group_for({"action": "分批买入/持有"}), "可以跟进持有")
        self.assertEqual(_group_for({"action": "观察"}), "暂不动作")
        self.assertEqual(_group_for({"action": "持有"}), "继续持有")


class ModelReviewBuildTest(unittest.TestCase):
    def test_build_report_with_empty_and_sample_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fund_path = root / "fund.db"
            stock_path = root / "stock.db"
            FundDatabase(fund_path).init()
            StockDatabase(stock_path).init()

            record_predictions(
                fund_path,
                "2026-06-30",
                "review",
                [
                    {
                        "code": "000001",
                        "name": "测试基金",
                        "review_action": "分批买入/持有",
                        "recommendation_group": "可以跟进持有",
                        "price": 1.0,
                        "score": 80.0,
                        "evidence": {"score": 85.0},
                        "target_weight": 0.1,
                    }
                ],
            )
            with sqlite3.connect(fund_path) as connection:
                connection.execute(
                    """
                    UPDATE predictions
                    SET outcome_status='hit', outcome_return=1.2,
                        max_adverse=-0.5, outcome_date='2026-07-02'
                    WHERE horizon_days=2
                    """
                )

            config_path = root / "model-review-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "report_dir": str(root / "reports"),
                        "fund_db": str(fund_path),
                        "stock_db": str(stock_path),
                        "min_samples": 20,
                        "notify": {"macos": False, "webhook_url": ""},
                    }
                ),
                encoding="utf-8",
            )
            from fund_tracker.model_review import load_config

            config = load_config(config_path)
            path, summary, report, fund, stock = build_report(
                "2026-09-30",
                config,
                force=True,
            )
            self.assertTrue(path.exists())
            self.assertIn("模型表现复盘 · 2026-09", report)
            self.assertIn("测试基金", report)
            self.assertIn("样本不足", report)
            self.assertEqual(fund["total"], 3)
            self.assertEqual(fund["horizons"][2]["evaluated"], 1)
            self.assertEqual(fund["horizons"][2]["hit_rate"], 100.0)
            self.assertEqual(stock["total"], 0)
            self.assertIn("基金 3 条", summary)


class ModuleErrorLinesTest(unittest.TestCase):
    def test_error_lines_name_the_module_and_reason(self) -> None:
        from fund_tracker.model_review import _module_error_lines

        module = {"label": "基金", "error": "no such table: predictions"}
        lines = _module_error_lines(module)
        self.assertEqual(len(lines), 2)
        combined = "\n".join(lines)
        self.assertIn("基金", combined)
        self.assertIn("no such table: predictions", combined)
        self.assertIn("⚠️", combined)

    def test_default_text_when_error_missing(self) -> None:
        from fund_tracker.model_review import _module_error_lines

        lines = _module_error_lines({"label": "股票"})
        self.assertIn("数据库读取失败", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
