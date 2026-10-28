"""Tests for the encrypted backup helpers (stage/encrypt/retention)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from shared.backup import ensure_passphrase, prune_tier, run_backup


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE nav (code TEXT, date TEXT, unit_nav REAL)")
        connection.executemany(
            "INSERT INTO nav VALUES (?, ?, ?)",
            [(f"{i:06d}", "2026-10-27", 1.0) for i in range(rows)],
        )
        connection.commit()
    finally:
        connection.close()


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
        _make_db(self.tmp / "data/fund_tracker.db")
        (self.tmp / "data/holdings.json").write_text(
            '{"as_of":"2026-10-27","total_value":1,"cash":0,"items":[]}',
            encoding="utf-8",
        )
        (self.tmp / "config.json").write_text(
            '{"notify":{"webhook_url":"https://example/hook"}}', encoding="utf-8"
        )
        (self.tmp / "scripts/").mkdir(exist_ok=True)
        (self.tmp / "scripts/backup.py").write_text("# placeholder", encoding="utf-8")
        self.passfile = self.tmp / "pass"
        self.dest = self.tmp / "backups"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_passphrase_created_once_chmod_600(self) -> None:
        ensure_passphrase(self.passfile)
        first = self.passfile.read_text(encoding="utf-8")
        self.passfile.chmod(0o600)
        ensure_passphrase(self.passfile)
        self.assertEqual(self.passfile.read_text(encoding="utf-8"), first)

    def test_run_backup_roundtrip(self) -> None:
        # HOME sources are empty in this test; only staged items appear.
        fake_home = self.tmp / "fakehome"
        fake_home.mkdir(exist_ok=True)
        report = run_backup(self.tmp, dest=self.dest, passfile=self.passfile, home=fake_home)
        enc = Path(report["archive"])
        self.assertTrue(enc.exists())
        manifest = Path(report["manifest"])
        self.assertTrue(manifest.exists())
        self.assertIn("dbt-", enc.name)

        # tier-marks should exist; daily tier should have exactly one archive
        self.assertEqual(len(list((self.dest / "daily").glob("*.tar.enc"))), 1)
        self.assertTrue((self.dest / "tier-marks.json").exists())
        marks = (self.dest / "tier-marks.json").read_text(encoding="utf-8")
        self.assertIn("month_key", marks)

    def test_prune_tier_keeps_limit(self) -> None:
        (self.dest / "daily").mkdir(parents=True, exist_ok=True)
        for index in range(10):
            path = self.dest / "daily" / f"dbt-2026102{index}120000.tar.enc"
            path.write_bytes(b"fake")
        removed = prune_tier(self.dest, "daily", 7)
        self.assertEqual(removed, 3)
        self.assertEqual(len(list((self.dest / "daily").glob("*.tar.enc"))), 7)


if __name__ == "__main__":
    unittest.main()
