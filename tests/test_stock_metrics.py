from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from stock_tracker.metrics import (
    build_metrics,
    position_score,
    risk_score,
    trend_score,
)


def make_kline(
    *,
    days: int = 800,
    start_price: float = 100.0,
    daily_drift: float = 0.0004,
    amplitude: float = 0.012,
) -> list[dict]:
    rows = []
    current = date(2023, 1, 2)
    price = start_price
    index = 0
    while len(rows) < days:
        if current.weekday() < 5:
            close = price * (1 + daily_drift + amplitude * math.sin(index / 7))
            high = close * 1.01
            low = close * 0.99
            volume = 1_000_000 + 120_000 * abs(math.sin(index / 11))
            rows.append(
                {
                    "date": current.isoformat(),
                    "open": close * 0.999,
                    "close": round(close, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "volume": volume,
                    "amount": volume * close,
                    "amplitude": 2.0,
                    "pct": 0.0,
                    "change": 0.0,
                    "turnover": 1.0,
                }
            )
            price = close
            index += 1
        current += timedelta(days=1)
    return rows


def make_stock(kline: list[dict], **quote_overrides) -> dict:
    latest = kline[-1]
    quote = {
        "code": "600000",
        "name": "测试股票",
        "price": latest["close"],
        "high": latest["high"],
        "low": latest["low"],
        "open": latest["open"],
        "prev_close": kline[-2]["close"],
        "change": 0.5,
        "pct": 0.5,
        "amplitude": 2.0,
        "volume": latest["volume"],
        "amount": latest["amount"],
        "volume_ratio": 1.2,
        "turnover": 1.0,
        "pe": 15.0,
        "pb": 1.5,
        "market_cap": 1000 * 1e8,
        "float_market_cap": 800 * 1e8,
        "industry": "银行",
        "region": "上海",
        "concepts": "",
    }
    quote.update(quote_overrides)
    return {
        "code": "600000",
        "name": "测试股票",
        "quote": quote,
        "kline": kline,
        "financials": {
            "report_date": "2026-06-30",
            "roe": 12.0,
            "revenue_growth": 8.0,
            "profit_growth": 10.0,
            "debt_ratio": 45.0,
            "gross_margin": 35.0,
            "cash_ratio": 0.8,
            "eps": 1.2,
            "bps": 10.0,
        },
        "announcements": [],
    }


class StockMetricsTest(unittest.TestCase):
    def test_build_metrics_returns_bounded_scores(self) -> None:
        kline = make_kline()
        metrics = build_metrics(
            make_stock(kline),
            {"risk_free_rate": 1.5},
            today=date.fromisoformat(kline[-1]["date"]),
        )
        self.assertTrue(metrics["data_ok"])
        for key in (
            "score",
            "quality_score",
            "trend_score",
            "position_score",
            "risk_score",
        ):
            self.assertGreaterEqual(metrics[key], 0)
            self.assertLessEqual(metrics[key], 100)
        self.assertGreaterEqual(metrics["history_days"], 750)
        self.assertIsNotNone(metrics["ret_250d"])
        self.assertIsNotNone(metrics["dd_1y"])
        self.assertIsNotNone(metrics["volume_spike"])
        self.assertEqual(metrics["industry"], "银行")

    def test_empty_kline_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_metrics(
                {
                    "code": "600000",
                    "name": "空数据",
                    "quote": {},
                    "kline": [],
                    "financials": {},
                }
            )

    def test_trend_score_rewards_above_moving_averages(self) -> None:
        above = trend_score(
            {
                "above_ma20": True,
                "above_ma60": True,
                "ma20": 12.0,
                "ma60": 11.0,
                "rsi14": 55.0,
                "ret_20d": 3.0,
            }
        )
        below = trend_score(
            {
                "above_ma20": False,
                "above_ma60": False,
                "ma20": 9.0,
                "ma60": 10.0,
                "rsi14": 80.0,
                "ret_20d": -3.0,
            }
        )
        self.assertGreater(above, below)

    def test_position_score_prefers_low_range(self) -> None:
        self.assertGreater(
            position_score(15.0, -10.0, "反弹"),
            position_score(90.0, -2.0, "偏强"),
        )

    def test_risk_score_penalises_drawdown_and_staleness(self) -> None:
        healthy = risk_score(
            dd_1y=-8.0,
            vol_1y=18.0,
            market_cap_yi=1000.0,
            pe=15.0,
            name="测试",
            stale_days=1,
            from_high_1y=-5.0,
            trend="偏强",
        )
        risky = risk_score(
            dd_1y=-50.0,
            vol_1y=60.0,
            market_cap_yi=20.0,
            pe=-5.0,
            name="ST测试",
            stale_days=10,
            from_high_1y=-60.0,
            trend="偏弱",
        )
        self.assertGreater(healthy, risky)


if __name__ == "__main__":
    unittest.main()
