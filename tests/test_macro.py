from __future__ import annotations

import unittest

from fund_tracker.macro import (
    _news_tags,
    _parse_sina_quote,
    build_macro_section,
    classify_regime,
)


class MacroTest(unittest.TestCase):
    def test_news_tags_use_headline_only(self) -> None:
        tags = _news_tags(
            "美联储维持利率不变",
            "文章还提到地缘、油价、汇率和房地产，但标题只讲美联储。",
        )
        self.assertIn("海外利率", tags)
        self.assertNotIn("地缘", tags)
        self.assertNotIn("油价", tags)
        self.assertNotIn("汇率", tags)

    def test_foreign_central_bank_not_tagged_as_domestic_policy(self) -> None:
        tags = _news_tags("俄罗斯央行连续第二次维持利率不变")
        self.assertIn("海外利率", tags)
        self.assertNotIn("国内政策", tags)

    def test_gold_quote_name_and_date(self) -> None:
        payload = (
            "4005.832,,4014.000,4014.300,4050.200,3989.100,23:00:24,"
            "4060.400,4042.200,0,5,1,2026-10-23,纽约黄金,0"
        )
        quote = _parse_sina_quote("hf_GC", payload)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote["name"], "纽约黄金")
        self.assertEqual(quote["date"], "2026-10-23")
        self.assertAlmostEqual(quote["current"], 4005.832, places=3)

    def test_regime_and_section(self) -> None:
        macro = {
            "china": {"10y": 1.64, "2y": 1.24, "10y_5d_bp": -0.8},
            "global": {
                "us_10y": 4.96,
                "us_2y": 4.65,
                "us_10y_5d_bp": -14.0,
                "us_10y_2y_bp": 31.0,
                "us_10y_2y_5d_bp": -2.0,
                "usdcny": {"current": 6.67, "pct": -0.07},
                "gold": {"current": 4005.8, "pct": -1.34},
                "oil": {"current": 102.5, "pct": 3.06},
            },
            "indices": [
                {
                    "name": "沪深300",
                    "current": 4105.33,
                    "pct_5d": -1.57,
                    "tech": {
                        "ret_20d": -7.96,
                        "ma20": 4295.86,
                        "ma60": 4497.41,
                        "rsi14": 23.1,
                        "trend": "偏弱",
                    },
                },
                {
                    "name": "红利指数",
                    "current": 3209.4,
                    "pct_5d": -3.84,
                    "tech": {
                        "ret_20d": -1.52,
                        "ma20": 3298.4,
                        "ma60": 3281.5,
                        "rsi14": 45.3,
                        "trend": "偏弱",
                    },
                },
                {
                    "name": "创业板指",
                    "current": 2684.1,
                    "pct_5d": -3.18,
                    "tech": {
                        "ret_20d": -18.62,
                        "ma20": 3006.2,
                        "ma60": 3292.6,
                        "rsi14": 16.9,
                        "trend": "偏弱",
                    },
                },
            ],
            "news": [],
            "errors": [],
        }
        regime = classify_regime(macro)
        self.assertIn("国内低利率", regime["labels"])
        self.assertIn("海外高利率", regime["labels"])
        self.assertIn("A股偏弱", regime["labels"])
        self.assertIn("红利占优", regime["labels"])
        section = build_macro_section(macro)
        self.assertIn("## 二、宏观与外部环境", section)
        self.assertIn("中国 10Y 国债", section)
        self.assertIn("大盘与 K 线", section)


if __name__ == "__main__":
    unittest.main()
