from __future__ import annotations

import unittest

from fund_tracker.evidence import build_evidence
from fund_tracker.report import _evidence_conclusion, _review_action


MACRO = {
    "china": {"10y": 1.64, "2y": 1.24, "10y_5d_bp": -0.8},
    "global": {
        "us_10y": 4.96,
        "us_2y": 4.65,
        "us_10y_5d_bp": -14.0,
        "usdcny": {"current": 6.67, "pct": -0.07},
        "oil": {"current": 102.5, "pct": 3.06},
    },
    "indices": [
        {
            "name": "沪深300",
            "tech": {"trend": "偏弱", "ret_20d": -7.96, "rsi14": 23.1},
        },
        {
            "name": "红利指数",
            "tech": {"trend": "偏弱", "ret_20d": -1.52, "rsi14": 45.3},
        },
        {
            "name": "创业板指",
            "tech": {"trend": "偏弱", "ret_20d": -18.62, "rsi14": 16.9},
        },
    ],
    "news": [
        {"title": "地缘冲突升级", "tags": ["地缘"]},
        {"title": "油价上涨", "tags": ["油价"]},
        {"title": "美联储偏鹰", "tags": ["海外利率"]},
    ],
}


class EvidenceTest(unittest.TestCase):
    def test_bond_evidence_is_high_and_bounded(self) -> None:
        rows = [
            {
                "code": "006980",
                "name": "纯债",
                "category": "纯债",
                "score": 97.2,
                "target_weight": 0.1,
                "dd_1y": -0.84,
                "from_high_1y": 0.0,
                "ret_1m": 0.3,
                "holdings": {"top_bond_weight": 16.9},
                "announcements": [],
            }
        ]
        evidence = build_evidence(rows, MACRO, {})
        item = evidence["006980"]
        self.assertGreaterEqual(item["score"], 80)
        self.assertLessEqual(item["score"], 100)
        self.assertEqual(item["grade"], "A")
        self.assertIn("parts", item)

    def test_high_alert_reduces_risk_score(self) -> None:
        row = {
            "code": "000047",
            "name": "观察债",
            "category": "二级债/固收+",
            "score": 79.0,
            "target_weight": 0,
            "dd_1y": -8.16,
            "from_high_1y": -3.0,
            "ret_1m": -0.5,
            "holdings": {"top_stock_weight": 11.7, "top_bond_weight": 35.3},
            "announcements": [],
        }
        alerts = {
            "000047": [
                {"level": "high", "message": "近 1 年最大回撤 -8.16%"}
            ]
        }
        evidence = build_evidence([row], MACRO, alerts)["000047"]
        self.assertLess(evidence["parts"]["risk"], 100)

    def test_daily_conclusion_for_held_fund_with_mid_evidence(self) -> None:
        row = {"code": "006839", "target_weight": 0.1}
        evidence = {
            "006839": {"score": 62.9, "grade": "C", "label": "证据一般"}
        }
        self.assertEqual(
            _evidence_conclusion(row, evidence),
            "持有、暂不加重",
        )

    def test_daily_conclusion_for_observation_with_mid_evidence(self) -> None:
        row = {"code": "007751", "target_weight": 0}
        evidence = {
            "007751": {"score": 75.2, "grade": "B", "label": "证据中上"}
        }
        self.assertEqual(_evidence_conclusion(row, evidence), "观察")

    def test_held_fund_with_mid_evidence_is_hold(self) -> None:
        row = {
            "score": 87.5,
            "target_weight": 0.1,
            "ret_5d": 0.08,
        }
        action, reason = _review_action(
            row,
            [],
            {"score": 62.9, "grade": "C", "label": "证据一般"},
        )
        self.assertEqual(action, "持有")
        self.assertIn("暂不加重", reason)

    def test_held_fund_with_weak_evidence_needs_review(self) -> None:
        row = {
            "score": 87.5,
            "target_weight": 0.1,
            "ret_5d": 0.08,
        }
        action, _ = _review_action(
            row,
            [],
            {"score": 55.0, "grade": "D", "label": "证据弱"},
        )
        self.assertEqual(action, "减仓/复核")

    def test_two_day_pullback_reason_matches_window(self) -> None:
        row = {
            "score": 90.0,
            "target_weight": 0,
            "ret_window": -3.0,
            "period_days": 2,
        }
        action, reason = _review_action(
            row,
            [],
            {"score": 85.0, "grade": "A", "label": "证据强"},
        )
        self.assertEqual(action, "小仓试探")
        self.assertIn("2 日回撤", reason)

    def test_observation_fund_with_strong_evidence_waits_for_pullback(self) -> None:
        row = {
            "score": 91.3,
            "target_weight": 0,
            "ret_5d": 0.06,
        }
        action, reason = _review_action(
            row,
            [],
            {"score": 91.0, "grade": "A", "label": "证据强"},
        )
        self.assertEqual(action, "观察")
        self.assertIn("没有回调", reason)

    def test_observation_fund_with_strong_evidence_and_pullback(self) -> None:
        row = {
            "score": 90.0,
            "target_weight": 0,
            "ret_5d": -3.0,
        }
        action, _ = _review_action(
            row,
            [],
            {"score": 85.0, "grade": "A", "label": "证据强"},
        )
        self.assertEqual(action, "小仓试探")


if __name__ == "__main__":
    unittest.main()
