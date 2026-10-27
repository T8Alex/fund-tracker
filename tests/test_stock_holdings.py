from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_tracker.holdings import attach_holdings, load_holdings


class StockHoldingsTest(unittest.TestCase):
    def test_missing_holdings_file_is_not_loaded(self) -> None:
        config = {"holdings_file": "/tmp/definitely-not-a-real-stock-holdings.json"}
        raw = load_holdings(config)
        self.assertFalse(raw["loaded"])
        rows = [{"code": "600000", "price": 10.0, "target_weight": 0.1}]
        result = attach_holdings(rows, raw)
        self.assertFalse(result["loaded"])
        self.assertIsNone(rows[0]["actual_weight"])

    def test_attach_holdings_computes_value_weight_and_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdings.json"
            path.write_text(
                json.dumps(
                    {
                        "as_of": "2026-10-23",
                        "cash": 1000.0,
                        "items": [
                            {
                                "code": "600000",
                                "shares": 100,
                                "cost_price": 8.0,
                            },
                            {
                                "code": "000001",
                                "shares": 50,
                                "cost_price": 20.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = {"holdings_file": str(path)}
            raw = load_holdings(config)
            self.assertTrue(raw["loaded"])
            rows = [
                {
                    "code": "600000",
                    "name": "测试银行",
                    "price": 10.0,
                    "target_weight": 0.5,
                },
                {
                    "code": "000001",
                    "name": "测试成长",
                    "price": 15.0,
                    "target_weight": 0.5,
                },
            ]
            result = attach_holdings(rows, raw)
            # Cash 1000 + 100*10 + 50*15 = 2750.
            self.assertAlmostEqual(result["total_value"], 2750.0)
            self.assertAlmostEqual(rows[0]["current_value"], 1000.0)
            self.assertAlmostEqual(rows[0]["pnl"], 200.0)
            self.assertAlmostEqual(rows[0]["pnl_pct"], 25.0)
            self.assertAlmostEqual(rows[0]["actual_weight"], 1000.0 / 2750.0)
            self.assertAlmostEqual(rows[1]["pnl"], -250.0)
            self.assertAlmostEqual(result["cash_weight"], 1000.0 / 2750.0)
            self.assertEqual(result["attached_count"], 2)

    def test_unmatched_holding_is_reported_and_optional_value_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdings.json"
            path.write_text(
                json.dumps(
                    {
                        "cash": 100.0,
                        "items": [
                            {
                                "code": "999999",
                                "shares": 10,
                                "current_value": 500.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            raw = load_holdings({"holdings_file": str(path)})
            rows = [{"code": "600000", "price": 10.0, "target_weight": 0.1}]
            result = attach_holdings(rows, raw)
            self.assertEqual(result["unmatched"], ["999999"])
            self.assertAlmostEqual(result["total_value"], 600.0)
            self.assertEqual(rows[0]["actual_weight"], 0.0)

    def test_loaded_holdings_without_match_sets_zero_weight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdings.json"
            path.write_text(
                json.dumps({"cash": 100.0, "items": []}),
                encoding="utf-8",
            )
            raw = load_holdings({"holdings_file": str(path)})
            self.assertTrue(raw["loaded"])
            rows = [{"code": "600000", "price": 10.0, "target_weight": 0.1}]
            result = attach_holdings(rows, raw)
            self.assertEqual(result["attached_count"], 0)
            self.assertEqual(rows[0]["actual_weight"], 0.0)
            self.assertEqual(rows[0]["current_value"], 0.0)


if __name__ == "__main__":
    unittest.main()
