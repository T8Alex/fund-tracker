from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from unittest.mock import patch

from stock_tracker.cli import (
    _attach_meta,
    _build_review_rows,
    _drop_reused_webhook,
    _last_weekday_of_month,
    _review_due_info,
    _summary_period_key,
    _webhook_reuses_fund,
    _weight,
    _weight_text,
)
from stock_tracker.store import Database


class StockCliRulesTest(unittest.TestCase):
    def test_weight_accepts_decimal_and_percent(self) -> None:
        self.assertAlmostEqual(_weight(0.12), 0.12)
        self.assertAlmostEqual(_weight(12), 0.12)
        self.assertEqual(_weight_text(0.12), "12.0%")
        with self.assertRaises(ValueError):
            _weight(200)

    def test_summary_period_keys(self) -> None:
        self.assertEqual(
            _summary_period_key("weekly", date(2026, 10, 23)),
            "2026-W43",
        )
        self.assertEqual(
            _summary_period_key("monthly", date(2026, 10, 30)),
            "2026-10",
        )
        self.assertEqual(
            _last_weekday_of_month(date(2026, 10, 30)),
            date(2026, 10, 30),
        )

    def test_review_due_after_two_trading_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "stock.db")
            database.init()
            database.save_prices(
                "600000",
                [
                    {"date": "2026-10-23", "close": 10.0},
                    {"date": "2026-10-26", "close": 10.1},
                    {"date": "2026-10-27", "close": 10.2},
                ],
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
                database, "2026-10-26", config
            )
            self.assertFalse(due)
            self.assertEqual(days, 1)
            self.assertEqual(interval, 2)
            due, _, days, _, _ = _review_due_info(
                database, "2026-10-27", config
            )
            self.assertTrue(due)
            self.assertEqual(days, 2)

    def test_build_review_rows_adds_window_return_and_score_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "stock.db")
            database.init()
            database.upsert_stock(
                "600000",
                "测试银行",
                category="银行",
                role="核心",
                target_weight=0.1,
            )
            database.save_prices(
                "600000",
                [
                    {"date": "2026-10-21", "close": 10.0},
                    {"date": "2026-10-22", "close": 10.2},
                    {"date": "2026-10-23", "close": 10.4},
                ],
            )
            database.save_snapshot(
                "600000",
                "2026-10-21",
                {"code": "600000", "name": "测试银行", "score": 70.0},
            )
            database.save_snapshot(
                "600000",
                "2026-10-23",
                {
                    "code": "600000",
                    "name": "测试银行",
                    "price": 10.4,
                    "score": 75.0,
                    "target_weight": 0.1,
                },
            )
            config = {"holdings_file": Path(temp_dir) / "holdings.json"}
            rows, holdings = _build_review_rows(
                database,
                config,
                "2026-10-23",
                "2026-10-21",
                window_days=2,
            )
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["ret_window"], 4.0)
            self.assertAlmostEqual(rows[0]["score_delta_window"], 5.0)
            self.assertEqual(rows[0]["category"], "银行")
            self.assertFalse(holdings["loaded"])

    def test_stock_webhook_must_not_reuse_fund_webhook(self) -> None:
        with patch(
            "stock_tracker.cli._fund_webhook_url",
            return_value="https://open.feishu.cn/open-apis/bot/v2/hook/fund",
        ):
            self.assertTrue(
                _webhook_reuses_fund(
                    {
                        "notify": {
                            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fund"
                        }
                    }
                )
            )
            config = {
                "notify": {
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fund"
                }
            }
            self.assertTrue(_drop_reused_webhook(config))
            self.assertEqual(config["notify"]["webhook_url"], "")

    def test_attach_meta_sets_category(self) -> None:
        row = {"code": "600000", "industry": "银行"}
        _attach_meta(
            row,
            {
                "name": "测试银行",
                "category": "银行/防御",
                "role": "核心",
                "target_weight": 0.1,
            },
        )
        self.assertEqual(row["category"], "银行/防御")
        self.assertEqual(row["role"], "核心")
        self.assertEqual(row["target_weight"], 0.1)


if __name__ == "__main__":
    unittest.main()
