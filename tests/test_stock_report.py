from __future__ import annotations

import unittest

from stock_tracker.evidence import build_evidence
from stock_tracker.report import (
    _buy_range,
    _group_section,
    _recommendation_bucket,
    _review_action,
    _sell_range,
    generate_daily_report,
    generate_review,
    generate_summary_report,
)


def make_row(**overrides):
    row = {
        "code": "600000",
        "name": "测试银行",
        "price": 50.0,
        "price_date": "2026-10-23",
        "daily_pct": 0.5,
        "ret_5d": 1.0,
        "ret_20d": 2.0,
        "ret_250d": 5.0,
        "ret_window": 1.0,
        "period_days": 2,
        "period_label": "2 日",
        "score": 85.0,
        "quality_score": 80.0,
        "trend_score": 80.0,
        "position_score": 60.0,
        "risk_score": 80.0,
        "range_low_1y": 40.0,
        "range_high_1y": 60.0,
        "pe": 10.0,
        "pb": 1.2,
        "market_cap_yi": 1000.0,
        "industry": "银行",
        "trend_label": "偏强",
        "risk_label": "风险较低",
        "target_weight": 0.10,
        "actual_weight": None,
        "cost_price": None,
        "announcements": [],
    }
    row.update(overrides)
    return row


class StockActionRulesTest(unittest.TestCase):
    def test_no_holding_in_buy_zone_can_follow(self) -> None:
        row = make_row(price=45.0)
        action, _ = _review_action(row, [], {"score": 80.0})
        self.assertEqual(action, "分批买入/持有")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "可以跟进持有",
        )

    def test_no_holding_above_buy_zone_does_not_chase(self) -> None:
        row = make_row(price=55.0)
        action, _ = _review_action(row, [], {"score": 80.0})
        self.assertEqual(action, "观察")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "暂不动作",
        )

    def test_actual_position_at_target_continues_to_hold(self) -> None:
        row = make_row(actual_weight=0.10, target_weight=0.10)
        action, _ = _review_action(row, [], {"score": 80.0})
        self.assertEqual(action, "持有")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "继续持有",
        )

    def test_actual_position_over_target_needs_adjustment(self) -> None:
        row = make_row(actual_weight=0.20, target_weight=0.10)
        action, _ = _review_action(row, [], {"score": 80.0})
        self.assertEqual(action, "减仓/复核")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "可以调整",
        )

    def test_actual_position_with_zero_target_is_sell(self) -> None:
        row = make_row(actual_weight=0.10, target_weight=0.0)
        action, _ = _review_action(row, [], {"score": 80.0})
        self.assertEqual(action, "剔除")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "可以出货",
        )

    def test_severe_alert_on_actual_position_is_sell(self) -> None:
        row = make_row(actual_weight=0.10, target_weight=0.10)
        alerts = [{"level": "high", "message": "立案调查"}]
        action, _ = _review_action(row, alerts, {"score": 80.0})
        self.assertEqual(action, "出货/复核")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 80.0}),
            "可以出货",
        )

    def test_severe_alert_without_holding_needs_review_not_adjustment(self) -> None:
        row = make_row(actual_weight=None, target_weight=0.10)
        alerts = [{"level": "high", "message": "公告：立案调查"}]
        action, _ = _review_action(row, alerts, {"score": 70.0})
        self.assertEqual(action, "复核")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 70.0}),
            "暂不动作",
        )

    def test_historical_drawdown_alert_does_not_force_sell(self) -> None:
        row = make_row(actual_weight=0.10, target_weight=0.10)
        alerts = [{"level": "high", "message": "近1年最大回撤 -25%"}]
        action, _ = _review_action(row, alerts, {"score": 85.0})
        self.assertEqual(action, "持有")
        self.assertEqual(
            _recommendation_bucket(row, action, {"score": 85.0}),
            "继续持有",
        )

    def test_sell_range_does_not_tell_non_holder_to_sell(self) -> None:
        text = _sell_range(make_row(price=57.0, actual_weight=None))
        self.assertIn("未持有", text)
        self.assertNotIn("分批减 1/3", text)

    def test_buy_range_below_zone_avoids_catching_falling_knife(self) -> None:
        text = _buy_range(make_row(price=30.0))
        self.assertIn("低于区间", text)
        self.assertIn("不接飞刀", text)


class StockReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            make_row(),
            make_row(
                code="000001",
                name="测试成长",
                price=30.0,
                industry="电池",
                target_weight=0.10,
                score=55.0,
                quality_score=55.0,
                trend_score=30.0,
                risk_score=45.0,
                range_low_1y=20.0,
                range_high_1y=60.0,
            ),
        ]
        self.evidence = build_evidence(self.rows, {})
        self.market = [
            {
                "name": "沪深300",
                "current": 4100.0,
                "pct": 1.0,
                "pct_window": 0.5,
            }
        ]
        self.alerts = [
            {
                "code": "000001",
                "name": "测试成长",
                "level": "warn",
                "message": "单日波动较大",
            }
        ]

    def test_group_section_always_has_four_groups(self) -> None:
        text = _group_section(self.rows)
        for label in ("继续持有", "可以跟进持有", "可以调整", "可以出货"):
            self.assertIn(label, text)

    def test_daily_report_is_mobile_readable_and_disclaimed(self) -> None:
        text = generate_daily_report(
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            holdings={"loaded": False},
        )
        self.assertIn("股票追踪日报", text)
        self.assertIn("四组预览", text)
        self.assertIn("证据分不是胜率", text)
        self.assertIn("不构成投资建议", text)

    def test_daily_report_inserts_ai_section(self) -> None:
        text = generate_daily_report(
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            holdings={"loaded": False},
            ai_section="## AI 新闻与文章摘要（本地模型）\n\n测试 AI 摘要\n",
        )
        self.assertIn("AI 新闻与文章摘要（本地模型）", text)
        self.assertLess(
            text.index("AI 新闻与文章摘要（本地模型）"),
            text.index("## 三、观察池概览"),
        )

    def test_daily_report_lists_failed_stocks(self) -> None:
        text = generate_daily_report(
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            holdings={"loaded": False},
            errors=["600000 测试银行: 行情接口不可用"],
        )
        self.assertIn("数据异常", text)
        self.assertIn("600000 测试银行: 行情接口不可用", text)

    def test_review_has_reference_ranges_and_evidence_panel(self) -> None:
        text = generate_review(
            "2026-10-23",
            "2026-10-21",
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            window_days=2,
            holdings={"loaded": False},
        )
        self.assertIn("股票追踪 · 2 日推荐", text)
        self.assertIn("证据面板", text)
        self.assertIn("参考买入区间", text)
        self.assertIn("参考卖出区间", text)
        self.assertIn("暂不动作", text)

    def test_review_has_three_principles_and_learning_section(self) -> None:
        text = generate_review(
            "2026-10-23",
            "2026-10-21",
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            window_days=2,
            holdings={"loaded": False},
            learning_section="## 学习与复盘（自动）\n\n测试学习记录\n",
        )
        self.assertIn("## 三原则卡（行动 / 坚持 / 眼光）", text)
        self.assertIn("证伪", text)
        self.assertIn("## 学习与复盘（自动）", text)
        self.assertIn("测试学习记录", text)

    def test_summary_has_weekly_title(self) -> None:
        text = generate_summary_report(
            "weekly",
            "2026-10-23",
            "2026-10-16",
            "2026-10-23",
            self.rows,
            self.market,
            self.alerts,
            {},
            macro_section="## 二、宏观与外部环境\n\n测试\n",
            evidence=self.evidence,
            holdings={"loaded": False},
        )
        self.assertIn("股票追踪 · 周度总结", text)
        self.assertIn("四组结论", text)


if __name__ == "__main__":
    unittest.main()
