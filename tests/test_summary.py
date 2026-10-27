from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fund_tracker.cli import (
    _build_period_rows,
    _generate_summary_if_due,
    _last_weekday_of_month,
    _summary_period_key,
)
from fund_tracker.report import (
    _group_counts,
    generate_summary_report,
    next_month_last_weekday,
    summary_text_summary,
)
from fund_tracker.store import Database


class SummaryRulesTest(unittest.TestCase):
    def test_last_weekday_of_month(self) -> None:
        self.assertEqual(_last_weekday_of_month(date(2026, 10, 24)), date(2026, 10, 30))
        self.assertEqual(_last_weekday_of_month(date(2026, 11, 1)), date(2026, 11, 30))
        self.assertEqual(next_month_last_weekday("2026-10-30"), "2026-11-30")

    def test_period_keys(self) -> None:
        self.assertEqual(_summary_period_key("weekly", date(2026, 10, 23)), "2026-W43")
        self.assertEqual(_summary_period_key("monthly", date(2026, 10, 30)), "2026-10")

    def test_period_rows_compute_month_return(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.upsert_fund(
                "000001",
                "测试基金",
                category="纯债",
                role="核心",
                target_weight=0.2,
            )
            for day, nav in [
                (date(2026, 10, 1), 1.00),
                (date(2026, 10, 9), 1.01),
                (date(2026, 10, 23), 1.05),
            ]:
                database.save_nav("000001", [(day, nav, 0.0)], [(day, nav)])
            database.save_snapshot(
                "000001",
                "2026-09-30",
                {"code": "000001", "name": "测试基金", "score": 70.0},
            )
            database.save_snapshot(
                "000001",
                "2026-10-23",
                {"code": "000001", "name": "测试基金", "score": 80.0},
            )

            rows = _build_period_rows(
                database,
                "2026-10-23",
                "2026-09-30",
                "2026-10-01",
                "本月",
            )

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ret_window"], 5.0)
        self.assertEqual(rows[0]["period_label"], "本月")
        self.assertEqual(rows[0]["nav_start_date"], "2026-10-01")
        self.assertAlmostEqual(rows[0]["score_delta_window"], 10.0)

    def test_summary_report_has_four_groups(self) -> None:
        rows = [
            {
                "code": "A",
                "name": "债基A",
                "category": "纯债",
                "target_weight": 0.2,
                "nav": 1.0,
                "score": 90.0,
                "ret_window": 0.1,
                "period_label": "本周",
            },
            {
                "code": "C",
                "name": "二级债C",
                "category": "二级债/固收+",
                "target_weight": 0.1,
                "nav": 1.0,
                "score": 75.0,
                "ret_window": -0.5,
                "period_label": "本周",
            },
            {
                "code": "D",
                "name": "预警D",
                "category": "红利",
                "target_weight": 0.1,
                "nav": 1.0,
                "score": 90.0,
                "ret_window": 0.2,
                "period_label": "本周",
            },
            {
                "code": "E",
                "name": "观察E",
                "category": "宽基",
                "target_weight": 0.0,
                "nav": 1.0,
                "score": 70.0,
                "ret_window": 0.0,
                "period_label": "本周",
            },
        ]
        evidence = {
            "A": {"score": 90.0, "grade": "A", "label": "证据强"},
            "C": {"score": 65.0, "grade": "C", "label": "证据一般"},
            "D": {"score": 85.0, "grade": "A", "label": "证据强"},
            "E": {"score": 60.0, "grade": "C", "label": "证据一般"},
        }
        alerts = [
            {
                "code": "D",
                "name": "预警D",
                "level": "high",
                "message": "基金经理变更：张三 → 李四",
            }
        ]
        market = [
            {
                "name": "沪深300",
                "current": 4100.0,
                "pct": 0.5,
                "pct_window": -1.0,
            }
        ]
        text = generate_summary_report(
            "weekly",
            "2026-10-23",
            "2026-10-16",
            "2026-10-23",
            rows,
            market,
            alerts,
            {"review": {"interval_trading_days": 2}},
            next_report_date="2026-10-30",
            macro_section="## 二、宏观与外部环境\n\n测试宏观。\n",
            evidence=evidence,
            learning_section="## 学习与复盘（自动）\n\n测试基金学习\n",
            ai_section="## AI 周度复盘（本地模型）\n\n测试 AI 周度摘要\n",
        )
        self.assertIn("# 周度总结推荐 · 2026-10-23", text)
        self.assertIn("AI 周度复盘（本地模型）", text)
        self.assertLess(
            text.index("AI 周度复盘（本地模型）"),
            text.index("## 三、组合本周表现"),
        )
        self.assertIn("## 四、四组结论", text)
        self.assertIn("**继续持有", text)
        self.assertIn("**可以跟进持有", text)
        self.assertIn("**可以调整", text)
        self.assertIn("**可以出货", text)
        self.assertIn("**暂不动作（观察）", text)
        self.assertIn("## 七、执行清单", text)
        self.assertIn("证据分不是胜率", text)
        self.assertIn("## 三原则卡（行动 / 坚持 / 眼光）", text)
        self.assertIn("## 学习与复盘（自动）", text)
        self.assertIn("测试基金学习", text)
        self.assertIn("下一次总结：**2026-10-30**", text)

        summary = summary_text_summary(
            "weekly",
            "2026-10-23",
            rows,
            Path("weekly.md"),
            alerts,
            evidence=evidence,
        )
        self.assertIn("周度总结 2026-10-23", summary)
        self.assertIn("继续持有", summary)
        self.assertIn("可以出货", summary)

    def test_group_counts(self) -> None:
        rows = [
            {"recommendation_group": "继续持有"},
            {"recommendation_group": "可以跟进持有"},
            {"recommendation_group": "可以跟进持有"},
            {"recommendation_group": "可以出货"},
        ]
        counts = _group_counts(rows)
        self.assertEqual(counts["继续持有"], 1)
        self.assertEqual(counts["可以跟进持有"], 2)
        self.assertEqual(counts["可以调整"], 0)
        self.assertEqual(counts["可以出货"], 1)

    def test_weekly_summary_is_generated_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.upsert_fund(
                "A",
                "测试基金",
                category="纯债",
                role="核心",
                target_weight=0.2,
            )
            for day, nav in [
                (date(2026, 10, 16), 1.00),
                (date(2026, 10, 23), 1.05),
            ]:
                database.save_nav("A", [(day, nav, 0.0)], [(day, nav)])
            database.save_snapshot(
                "A",
                "2026-10-23",
                {
                    "code": "A",
                    "name": "测试基金",
                    "score": 90.0,
                    "nav": 1.05,
                    "target_weight": 0.2,
                },
            )
            config = {
                "report_dir": str(Path(temp_dir) / "reports"),
                "summary": {
                    "enabled": True,
                    "weekly_enabled": True,
                    "monthly_enabled": True,
                },
                "review": {"interval_trading_days": 2},
            }
            market = [
                {
                    "name": "沪深300",
                    "current": 4100.0,
                    "pct": 0.5,
                    "pct_window": -1.0,
                }
            ]
            evidence = {"A": {"score": 90.0, "grade": "A", "label": "证据强"}}

            with patch(
                "fund_tracker.cli.fetch_market_history_range",
                return_value=market,
            ), patch(
                "fund_tracker.cli.build_evidence",
                return_value=evidence,
            ):
                result = _generate_summary_if_due(
                    "weekly",
                    database,
                    config,
                    None,
                    "2026-10-23",
                    macro_snapshot={},
                    macro_section="",
                )
                result_again = _generate_summary_if_due(
                    "weekly",
                    database,
                    config,
                    None,
                    "2026-10-23",
                    macro_snapshot={},
                    macro_section="",
                )

            self.assertIsNotNone(result)
            self.assertIsNone(result_again)
            path, summary, rows = result
            self.assertTrue(path.exists())
            self.assertIn("周度总结", summary)
            self.assertEqual(len(rows), 1)
            stored = database.get_summary_report("weekly", "2026-W43")
            self.assertIsNotNone(stored)
            self.assertEqual(stored["content_path"], str(path))

    def test_monthly_summary_is_generated_on_last_weekday(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.upsert_fund(
                "A",
                "测试基金",
                category="纯债",
                role="核心",
                target_weight=0.2,
            )
            for day, nav in [
                (date(2026, 10, 1), 1.00),
                (date(2026, 10, 23), 1.05),
            ]:
                database.save_nav("A", [(day, nav, 0.0)], [(day, nav)])
            database.save_snapshot(
                "A",
                "2026-10-23",
                {
                    "code": "A",
                    "name": "测试基金",
                    "score": 90.0,
                    "nav": 1.05,
                    "target_weight": 0.2,
                },
            )
            config = {
                "report_dir": str(Path(temp_dir) / "reports"),
                "summary": {
                    "enabled": True,
                    "weekly_enabled": True,
                    "monthly_enabled": True,
                },
                "review": {"interval_trading_days": 2},
            }
            market = [
                {
                    "name": "沪深300",
                    "current": 4100.0,
                    "pct": 0.5,
                    "pct_window": -1.0,
                }
            ]
            evidence = {"A": {"score": 90.0, "grade": "A", "label": "证据强"}}

            with patch(
                "fund_tracker.cli.fetch_market_history_range",
                return_value=market,
            ), patch(
                "fund_tracker.cli.build_evidence",
                return_value=evidence,
            ):
                result = _generate_summary_if_due(
                    "monthly",
                    database,
                    config,
                    None,
                    "2026-10-30",
                    macro_snapshot={},
                    macro_section="",
                )

            self.assertIsNotNone(result)
            path, summary, _ = result
            self.assertTrue(path.exists())
            self.assertIn("月度总结", summary)
            self.assertEqual(path.name, "monthly-2026-10.md")
            stored = database.get_summary_report("monthly", "2026-10")
            self.assertIsNotNone(stored)
            self.assertEqual(stored["report_date"], "2026-10-30")

    def test_summary_report_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.save_summary_report(
                "weekly",
                "2026-W43",
                "2026-10-23",
                "2026-10-16",
                "2026-10-23",
                "测试摘要",
                "weekly.md",
            )
            stored = database.get_summary_report("weekly", "2026-W43")
            last = database.get_last_summary_report("weekly")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["summary"], "测试摘要")
        self.assertIsNotNone(last)
        self.assertEqual(last["report_date"], "2026-10-23")


if __name__ == "__main__":
    unittest.main()
