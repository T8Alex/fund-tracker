from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fund_tracker.cli import exit_code_for_status
from fund_tracker.report import _evidence_table
from fund_tracker.store import Database
from shared.formula import FORMULA_VERSION


LEGACY_SOURCE_HEALTH = """
CREATE TABLE source_health (
    source TEXT NOT NULL,
    run_date TEXT NOT NULL,
    fund_code TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    errors TEXT,
    PRIMARY KEY (source, run_date, fund_code)
)
"""


class SourceHealthUniquenessTest(unittest.TestCase):
    def test_null_fund_code_rows_do_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            for _ in range(5):
                database.save_source_health("macro", "2026-10-29", None, True)
            rows = database.get_source_health("2026-10-29")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["fund_code"], "")

    def test_legacy_table_is_rebuilt_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(str(path))
            try:
                connection.executescript(LEGACY_SOURCE_HEALTH)
                connection.executemany(
                    "INSERT INTO source_health(source, run_date, fund_code, success) "
                    "VALUES(?, ?, ?, ?)",
                    [("market_index", "2026-10-29", None, 1) for _ in range(9)],
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            database.init()

            rows = database.get_source_health("2026-10-29")
            self.assertEqual(len(rows), 1, "legacy NULL rows must collapse to one")
            connection = sqlite3.connect(str(path))
            try:
                info = {
                    row[1]: row
                    for row in connection.execute("PRAGMA table_info(source_health)")
                }
                self.assertEqual(info["fund_code"][3], 1, "fund_code must be NOT NULL")
            finally:
                connection.close()

            # Re-running init() must be a no-op, not a second rebuild.
            database.init()
            self.assertEqual(len(database.get_source_health("2026-10-29")), 1)


class RunArtifactTest(unittest.TestCase):
    def test_market_and_macro_snapshots_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            market = [{"name": "沪深300", "close": 4041.84, "pct": -1.03}]
            macro = {"as_of": "2026-10-29", "news": [{"title": "测试"}]}
            database.save_artifact("2026-10-29", "market", market)
            database.save_artifact("2026-10-29", "macro", macro)

            self.assertEqual(database.get_artifact("2026-10-29", "market"), market)
            self.assertEqual(database.get_artifact("2026-10-29", "macro"), macro)
            self.assertIsNone(database.get_artifact("2026-10-28", "market"))

    def test_artifact_is_overwritten_per_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.save_artifact("2026-10-29", "market", [{"close": 1}])
            database.save_artifact("2026-10-29", "market", [{"close": 2}])
            self.assertEqual(
                database.get_artifact("2026-10-29", "market"), [{"close": 2}]
            )


class FormulaVersionTest(unittest.TestCase):
    def test_previous_snapshot_ignores_other_formula_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.save_snapshot(
                "000032", "2026-10-28", {"score": 70.0}, formula_version="ev-old"
            )
            previous = database.get_previous_snapshot(
                "000032", "2026-10-29", formula_version=FORMULA_VERSION
            )
            self.assertIsNone(previous)

            database.save_snapshot(
                "000032", "2026-10-28b", {"score": 72.0}
            )
            previous = database.get_previous_snapshot(
                "000032", "2026-10-29", formula_version=FORMULA_VERSION
            )
            self.assertEqual(previous["score"], 72.0)

    def test_snapshot_records_current_version_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            database.save_snapshot("000032", "2026-10-29", {"score": 60.0})
            connection = sqlite3.connect(str(Path(temp_dir) / "test.db"))
            try:
                stored = connection.execute(
                    "SELECT formula_version FROM snapshots WHERE code='000032'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(stored, FORMULA_VERSION)


class EvidenceTableTest(unittest.TestCase):
    def test_sub_scores_render_from_parts(self) -> None:
        rows = [
            {
                "name": "某基金",
                "code": "000032",
                "evidence": {
                    "score": 80.0,
                    "grade": "A",
                    "parts": {
                        "quality": 88.0,
                        "trend": 61.0,
                        "position": 72.0,
                        "macro": 65.0,
                        "risk": 90.0,
                    },
                },
            }
        ]
        table = _evidence_table(rows)
        self.assertIn("88.0", table)
        self.assertIn("61.0", table)
        self.assertIn("90.0", table)
        self.assertNotIn("| — | — |", table)

    def test_flat_evidence_still_renders(self) -> None:
        rows = [
            {
                "name": "某基金",
                "code": "000032",
                "evidence": {
                    "score": 70.0,
                    "grade": "B",
                    "quality": 71.0,
                    "trend": 55.0,
                    "position": 60.0,
                    "macro": 50.0,
                    "risk": 80.0,
                },
            }
        ]
        table = _evidence_table(rows)
        self.assertIn("71.0", table)
        self.assertIn("80.0", table)


class ExitCodeTest(unittest.TestCase):
    def test_status_mapping(self) -> None:
        self.assertEqual(exit_code_for_status("ok"), 0)
        self.assertEqual(exit_code_for_status("partial"), 2)
        self.assertEqual(exit_code_for_status("stale"), 3)
        self.assertEqual(exit_code_for_status("failed"), 1)
        self.assertEqual(exit_code_for_status("unknown"), 1)


if __name__ == "__main__":
    unittest.main()
