from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stock_tracker.store import Database


class StockStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "stock.db")
        self.database.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stock_crud(self) -> None:
        self.database.upsert_stock(
            "600000",
            "测试银行",
            category="银行",
            role="核心",
            target_weight=0.1,
            note="测试",
        )
        stocks = self.database.list_stocks()
        self.assertEqual(len(stocks), 1)
        self.assertEqual(stocks[0]["target_weight"], 0.1)
        self.assertEqual(self.database.get_stock("600000")["name"], "测试银行")

        self.database.upsert_stock("600000", "测试银行新", target_weight=0.2)
        updated = self.database.get_stock("600000")
        self.assertEqual(updated["name"], "测试银行新")
        self.assertEqual(updated["target_weight"], 0.2)

        self.assertTrue(self.database.remove_stock("600000"))
        self.assertFalse(self.database.remove_stock("600000"))
        self.assertEqual(self.database.list_stocks(), [])

    def test_prices_and_trading_day_count(self) -> None:
        self.database.save_prices(
            "600000",
            [
                {"date": "2026-10-21", "close": 10.0, "pct": 1.0, "turnover": 1.0},
                {"date": "2026-10-22", "close": 10.2, "pct": 2.0, "turnover": 1.2},
                {"date": "2026-10-23", "close": 10.1, "pct": -1.0, "turnover": 0.8},
            ],
        )
        self.assertEqual(self.database.latest_price_date(), "2026-10-23")
        self.assertEqual(
            self.database.count_trading_days_since("2026-10-21", "2026-10-23"),
            2,
        )
        window = self.database.get_price_window("600000", "2026-10-23", 2)
        self.assertEqual([row["date"] for row in window], ["2026-10-22", "2026-10-23"])
        self.assertEqual(
            self.database.get_price_on_or_before("600000", "2026-10-22")["close"],
            10.2,
        )
        self.assertEqual(
            len(self.database.get_price_range("600000", "2026-10-21", "2026-10-23")),
            3,
        )

    def test_snapshots_alerts_reviews_and_csv(self) -> None:
        metrics = {
            "code": "600000",
            "name": "测试银行",
            "industry": "银行",
            "price": 10.0,
            "daily_pct": 1.0,
            "ret_20d": 2.0,
            "ret_250d": 5.0,
            "dd_1y": -8.0,
            "vol_1y": 18.0,
            "pe": 10.0,
            "pb": 1.2,
            "market_cap_yi": 1000.0,
            "quality_score": 80.0,
            "trend_score": 70.0,
            "position_score": 60.0,
            "risk_score": 80.0,
            "score": 78.0,
        }
        self.database.save_snapshot("600000", "2026-10-22", {**metrics, "score": 80.0})
        self.database.save_snapshot("600000", "2026-10-23", metrics)
        self.assertEqual(
            self.database.get_previous_snapshot("600000", "2026-10-23")["score"],
            80.0,
        )
        self.assertEqual(len(self.database.get_snapshots("2026-10-23")), 1)
        self.assertEqual(
            self.database.get_snapshot_on_or_before("600000", "2026-10-23")["score"],
            78.0,
        )

        self.database.save_alerts(
            "2026-10-22",
            [{"code": "600000", "name": "测试银行", "level": "high", "message": "旧"}],
        )
        self.database.save_alerts(
            "2026-10-23",
            [{"code": "600000", "name": "测试银行", "level": "warn", "message": "新"}],
        )
        self.assertEqual(self.database.get_alerts("2026-10-23")[0]["level"], "warn")
        self.assertEqual(
            self.database.get_alerts_on_or_before("2026-10-24")[0]["message"],
            "新",
        )

        self.database.save_run("2026-10-23", "ok", "测试摘要")
        self.assertEqual(self.database.get_run("2026-10-23")["status"], "ok")

        self.database.save_review(
            "2026-10-23",
            "2026-10-21",
            "2026-10-23",
            "推荐摘要",
            "review.md",
        )
        self.assertEqual(self.database.get_last_review()["summary"], "推荐摘要")

        self.database.save_summary_report(
            "weekly",
            "2026-W43",
            "2026-10-23",
            "2026-10-16",
            "2026-10-23",
            "周度摘要",
            "weekly.md",
        )
        self.assertEqual(
            self.database.get_summary_report("weekly", "2026-W43")["summary"],
            "周度摘要",
        )

        csv_path = Path(self.temp_dir.name) / "out.csv"
        self.assertEqual(self.database.export_csv(csv_path, "2026-10-23"), 1)
        text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("600000", text)
        self.assertIn("测试银行", text)


if __name__ == "__main__":
    unittest.main()
