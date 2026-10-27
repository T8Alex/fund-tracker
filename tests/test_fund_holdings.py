from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fund_tracker.holdings import attach_holdings, load_holdings
from fund_tracker.report import _actual_holdings_section, _review_action


class FundHoldingsTest(unittest.TestCase):
    def test_load_and_attach_actual_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "holdings.json"
            path.write_text(
                json.dumps(
                    {
                        "as_of": "2026-10-23",
                        "total_value": 1000.0,
                        "total_pnl": -100.0,
                        "daily_pnl": 10.0,
                        "items": [
                            {
                                "code": "000001",
                                "market_value": 600.0,
                                "pnl": -60.0,
                                "daily_pct": 1.0,
                            },
                            {
                                "code": "000002",
                                "market_value": 400.0,
                                "pnl": -40.0,
                                "daily_pct": -0.5,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            holdings = load_holdings({"holdings_file": str(path)})
            self.assertTrue(holdings["loaded"])
            self.assertAlmostEqual(holdings["total_return_pct"], -9.0909, places=3)

            rows = [
                {"code": "000001", "name": "基金一", "target_weight": 0.5},
                {"code": "000002", "name": "基金二", "target_weight": 0.5},
                {"code": "000003", "name": "未持有", "target_weight": 0.0},
            ]
            result = attach_holdings(rows, holdings)
            self.assertAlmostEqual(rows[0]["actual_weight"], 0.6)
            self.assertAlmostEqual(rows[1]["actual_weight"], 0.4)
            self.assertEqual(rows[2]["actual_weight"], 0.0)
            self.assertEqual(result["attached_count"], 2)

            section = _actual_holdings_section(rows, result)
            self.assertIn("实际持仓与盈亏", section)
            self.assertIn("基金一", section)
            self.assertIn("60.0%", section)

    def test_actual_overweight_is_rebalance(self) -> None:
        row = {
            "code": "000001",
            "name": "基金一",
            "score": 90.0,
            "target_weight": 0.1,
            "actual_weight": 0.2,
        }
        action, reason = _review_action(row, [], {"score": 85.0})
        self.assertEqual(action, "再平衡")
        self.assertIn("高于目标", reason)

    def test_actual_zero_target_is_sell(self) -> None:
        row = {
            "code": "000001",
            "name": "基金一",
            "score": 90.0,
            "target_weight": 0.0,
            "actual_weight": 0.02,
        }
        action, reason = _review_action(row, [], {"score": 85.0})
        self.assertEqual(action, "剔除")
        self.assertIn("目标 0%", reason)

    def test_target_but_not_held_is_buy(self) -> None:
        row = {
            "code": "000001",
            "name": "基金一",
            "score": 82.0,
            "target_weight": 0.1,
            "actual_weight": 0.0,
        }
        action, reason = _review_action(row, [], {"score": 81.0})
        self.assertEqual(action, "分批买入/持有")
        self.assertIn("未持有", reason)


if __name__ == "__main__":
    unittest.main()
