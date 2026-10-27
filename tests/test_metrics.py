from __future__ import annotations

import unittest
from datetime import date, timedelta

from fund_tracker.metrics import (
    annualized_return,
    classify_category,
    max_drawdown,
    parse_manager,
    score_fund,
    sharpe,
    total_return,
    volatility,
)


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.start = date(2023, 1, 1)
        self.rising = [
            (self.start + timedelta(days=index), 100 * (1.0002 ** index))
            for index in range(1100)
        ]

    def test_total_return_positive(self) -> None:
        value = total_return(self.rising, 365)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_annualized_return_reasonable(self) -> None:
        value = annualized_return(self.rising, 1095)
        self.assertIsNotNone(value)
        self.assertGreater(value, 5)
        self.assertLess(value, 9)

    def test_max_drawdown(self) -> None:
        series = [
            (date(2024, 1, 1), 100),
            (date(2024, 2, 1), 120),
            (date(2024, 3, 1), 90),
            (date(2024, 4, 1), 110),
        ]
        self.assertAlmostEqual(max_drawdown(series, 365), -25.0, places=6)

    def test_volatility_positive(self) -> None:
        series = []
        for index in range(300):
            value = 100 + (1 if index % 2 else -1)
            series.append((self.start + timedelta(days=index), value))
        self.assertGreater(volatility(series, 365), 0)

    def test_sharpe_not_none(self) -> None:
        self.assertIsNotNone(sharpe(self.rising, 365, 1.5))

    def test_parse_manager(self) -> None:
        result = parse_manager(
            [
                {
                    "name": "张三",
                    "workTime": "6年又165天",
                    "fundSize": "220.39亿(14只基金)",
                    "star": 4,
                }
            ]
        )
        self.assertEqual(result["name"], "张三")
        self.assertAlmostEqual(result["years"], 6.45, places=2)
        self.assertEqual(result["fund_size_yi"], 220.39)

    def test_classify_category(self) -> None:
        self.assertEqual(
            classify_category("易方达信用债债券A", "债券型-信用债", {"stock": 0}),
            "纯债",
        )
        self.assertEqual(
            classify_category("招商安裕灵活配置混合A", "混合型-偏债", {"stock": 15}),
            "二级债/固收+",
        )
        self.assertEqual(
            classify_category("华泰柏瑞中证红利低波动ETF联接A", "指数型-股票", {"stock": 95}),
            "红利",
        )
        self.assertEqual(
            classify_category("易方达沪深300ETF联接A", "指数型-股票", {"stock": 95}),
            "宽基",
        )
        self.assertEqual(
            classify_category("易方达短债债券A", "债券型-短债", {"stock": 0}),
            "短债",
        )

    def test_score_bounds_and_order(self) -> None:
        good = {
            "ann_3y": 5.0,
            "dd_3y": -2.0,
            "sharpe_3y": 1.8,
            "manager_years": 6,
            "scale_yi": 20,
            "annual_fee_pct": 0.4,
            "history_years": 5,
        }
        bad = {
            "ann_3y": 0.5,
            "dd_3y": -12.0,
            "sharpe_3y": -0.2,
            "manager_years": 0.5,
            "scale_yi": 1,
            "annual_fee_pct": 1.8,
            "history_years": 5,
        }
        good_score, _ = score_fund(good, "纯债", 15)
        bad_score, _ = score_fund(bad, "纯债", 15)
        self.assertGreaterEqual(good_score, 0)
        self.assertLessEqual(good_score, 100)
        self.assertGreater(good_score, bad_score)


if __name__ == "__main__":
    unittest.main()
