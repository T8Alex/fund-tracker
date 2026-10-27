from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fund_tracker.learning import (
    evaluate_predictions,
    next_weekday_date,
    record_predictions,
    render_learning_section,
    stance_for_action,
    summarize_predictions,
)


def make_row(code: str, action: str, price: float) -> dict:
    return {
        "code": code,
        "name": f"测试{code}",
        "review_action": action,
        "recommendation_group": "暂不动作",
        "price": price,
        "nav": price,
        "score": 70.0,
        "target_weight": 0.1,
        "review_reason": "测试理由",
        "note": "测试逻辑",
        "evidence": {"score": 68.0, "grade": "C"},
    }


class LearningRulesTest(unittest.TestCase):
    def test_stance_mapping(self) -> None:
        self.assertEqual(stance_for_action("分批买入/持有"), "bullish")
        self.assertEqual(stance_for_action("持有"), "bullish")
        self.assertEqual(stance_for_action("减仓/复核"), "bearish")
        self.assertEqual(stance_for_action("剔除"), "bearish")
        self.assertEqual(stance_for_action("观察"), "neutral")
        self.assertEqual(stance_for_action("复核"), "neutral")

    def test_next_weekday_skips_weekend(self) -> None:
        self.assertEqual(next_weekday_date("2026-10-24", 2), "2026-10-27")
        self.assertEqual(next_weekday_date("2026-10-23", 20), "2026-11-20")

    def test_record_evaluate_and_summarize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "learning.db"
            rows = [
                make_row("600000", "持有", 100.0),
                make_row("600001", "减仓/复核", 100.0),
                make_row("600002", "观察", 100.0),
            ]
            self.assertEqual(
                record_predictions(
                    db_path,
                    "2026-10-24",
                    "review",
                    rows,
                    price_key="price",
                    horizons=(2,),
                ),
                3,
            )
            # Re-running the same report must not duplicate the log.
            self.assertEqual(
                record_predictions(
                    db_path,
                    "2026-10-24",
                    "review",
                    rows,
                    price_key="price",
                    horizons=(2,),
                ),
                3,
            )

            def loader(code: str, start: str, end: str):
                values = {
                    "600000": 110.0,
                    "600001": 90.0,
                    "600002": 95.0,
                }
                return [("2026-10-27", values[code])]

            self.assertEqual(
                evaluate_predictions(db_path, "2026-10-27", loader),
                3,
            )
            summary = summarize_predictions(
                db_path,
                as_of="2026-10-27",
                report_kind="review",
                module_label="测试",
            )
            bucket = summary["horizons"][2]
            self.assertEqual(summary["total"], 3)
            self.assertEqual(bucket["evaluated"], 3)
            self.assertEqual(bucket["hit"], 2)
            self.assertEqual(bucket["miss"], 0)
            self.assertEqual(bucket["neutral"], 1)
            self.assertAlmostEqual(bucket["hit_rate"], 100.0)
            self.assertAlmostEqual(bucket["avg_return"], -1.6667, places=3)
            text = render_learning_section(summary, "测试")
            self.assertIn("## 学习与复盘（自动）", text)
            self.assertIn("方向命中率", text)
            self.assertIn("观察", text)
            self.assertIn("观察类（不计命中率）", text)
            self.assertIn("我错在哪（自动提示）", text)
            self.assertIn("样本不足 20 条", text)

    def test_pending_prediction_is_not_scored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "learning.db"
            record_predictions(
                db_path,
                "2026-10-24",
                "review",
                [make_row("600000", "持有", 100.0)],
                price_key="price",
                horizons=(20,),
            )
            summary = summarize_predictions(
                db_path,
                as_of="2026-10-27",
                report_kind="review",
            )
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["pending"], 1)
            self.assertEqual(summary["horizons"][20]["evaluated"], 0)
            self.assertIsNone(summary["horizons"][20]["hit_rate"])


if __name__ == "__main__":
    unittest.main()
