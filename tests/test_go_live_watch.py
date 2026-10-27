from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.go_live_watch import (
    JOBS,
    TZ,
    capture,
    model_skip_recorded,
    render,
)


def at_day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 20, 35, tzinfo=TZ)


class ModelReviewGoLiveTest(unittest.TestCase):
    def capture_model(
        self,
        current: datetime,
        log_text: str,
        report_dir: Path,
    ) -> dict:
        launchd = {
            "loaded": True,
            "state": "not running",
            "runs": 2,
            "last_exit": "0",
        }
        with (
            patch("scripts.go_live_watch.now", return_value=current),
            patch("scripts.go_live_watch.launchd_state", return_value=launchd),
            patch("scripts.go_live_watch.read_tail", return_value=log_text),
            patch(
                "scripts.go_live_watch.model_state",
                return_value=[
                    {"port": 11434, "status": 200, "models": []},
                    {"port": 11436, "status": 200, "models": []},
                ],
            ),
            patch.dict(JOBS["model"], {"report_dir": report_dir}),
        ):
            return capture("model")

    def test_skip_record_must_match_today(self) -> None:
        log = "2026-10-26 不是本月最后一个工作日，跳过模型复盘。"
        self.assertTrue(model_skip_recorded(log, datetime(2026, 10, 26).date()))
        self.assertFalse(model_skip_recorded(log, datetime(2026, 10, 27).date()))

    def test_scheduled_skip_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = self.capture_model(
                at_day(2026, 10, 27),
                "2026-10-27 不是本月最后一个工作日，跳过模型复盘。",
                Path(directory),
            )

        self.assertEqual(data["verdict"], "SKIP")
        self.assertEqual(data["failures"], [])
        self.assertTrue(data["model_review"]["skipped"])
        self.assertFalse(data["model_review"]["due"])
        self.assertTrue(data["report_today"]["path"].endswith("model-review-2026-10.md"))
        text = render(data)
        self.assertIn("执行状态：SKIP", text)
        self.assertNotIn("runs 表：—", text)

    def test_old_skip_record_does_not_hide_a_missed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = self.capture_model(
                at_day(2026, 10, 27),
                "2026-10-26 不是本月最后一个工作日，跳过模型复盘。",
                Path(directory),
            )

        self.assertEqual(data["verdict"], "FAIL")
        self.assertIn(
            "模型复盘未运行，且没有今天的按规则跳过记录",
            data["failures"],
        )

    def test_due_model_review_without_report_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = self.capture_model(at_day(2026, 10, 30), "", Path(directory))

        self.assertEqual(data["verdict"], "FAIL")
        self.assertTrue(data["model_review"]["due"])
        self.assertIn("模型复盘今天应运行，但没有生成月度报告", data["failures"])

    def test_due_model_review_with_todays_report_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "model-review-2026-10.md"
            report.write_text("# 模型复盘\n", encoding="utf-8")
            timestamp = at_day(2026, 10, 30).timestamp()
            os.utime(report, (timestamp, timestamp))
            data = self.capture_model(at_day(2026, 10, 30), "", Path(directory))

        self.assertEqual(data["verdict"], "PASS")
        self.assertEqual(data["failures"], [])
        self.assertTrue(data["model_review"]["due"])
        self.assertFalse(data["model_review"]["skipped"])


if __name__ == "__main__":
    unittest.main()
