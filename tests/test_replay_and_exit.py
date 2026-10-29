from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fund_tracker.cli import _insert_replay_note
from shared.exit_codes import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_STALE,
    exit_code_for_status,
)
from stock_tracker.cli import _replay_note
from stock_tracker.store import Database


class ExitCodeTest(unittest.TestCase):
    def test_status_mapping(self) -> None:
        self.assertEqual(exit_code_for_status("ok"), EXIT_OK)
        self.assertEqual(exit_code_for_status("partial"), EXIT_PARTIAL)
        self.assertEqual(exit_code_for_status("stale"), EXIT_STALE)
        self.assertEqual(exit_code_for_status("failed"), EXIT_FAILED)

    def test_unknown_status_is_failure(self) -> None:
        self.assertEqual(exit_code_for_status("weird"), EXIT_FAILED)
        self.assertEqual(exit_code_for_status(None), EXIT_FAILED)


class ReplayNoteTest(unittest.TestCase):
    def test_missing_artifacts_produce_banner(self) -> None:
        with TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "stock.db")
            database.init()
            note = _replay_note(
                database,
                "2026-10-29",
                (("index_history", "指数历史"), ("macro", "宏观/新闻")),
            )
            self.assertIn("实时回退数据", note)
            self.assertIn("指数历史", note)
            self.assertIn("宏观/新闻", note)

    def test_saved_artifacts_produce_no_banner(self) -> None:
        with TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "stock.db")
            database.init()
            database.save_artifact("2026-10-29", "index_history", {"沪深300": []})
            database.save_artifact("2026-10-29", "macro", {"ok": True})
            self.assertEqual(
                _replay_note(
                    database,
                    "2026-10-29",
                    (("index_history", "指数历史"), ("macro", "宏观/新闻")),
                ),
                "",
            )


class FundReplayNoteTest(unittest.TestCase):
    def test_note_inserted_before_first_section(self) -> None:
        report = "# 日报\n\n## 一、市场快照\n\n内容\n"
        out = _insert_replay_note(report, ["行情", "宏观/新闻"])
        self.assertIn("本报告含实时回退数据", out)
        self.assertLess(out.index("本报告含实时回退数据"), out.index("## 一、"))

    def test_no_missing_keeps_report_unchanged(self) -> None:
        report = "# 日报\n\n## 一、市场快照\n"
        self.assertEqual(_insert_replay_note(report, []), report)


if __name__ == "__main__":
    unittest.main()
