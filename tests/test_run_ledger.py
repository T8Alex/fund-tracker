"""Tests for the append-only run ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.run_ledger import (
    RunLedger,
    events_for_day,
    last_event,
    load_events,
    prune,
)


class RunLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "run_ledger.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_and_read_back(self) -> None:
        ledger = RunLedger(self.path)
        ledger.record(job="fund-run", event="start")
        ledger.record(
            job="fund-run",
            event="done",
            status="ok",
            notify={"sent": True, "webhook": True},
            counts={"rows": 14},
            errors=["哎呀" * 500],
            data_date="2026-10-27",
        )
        events = load_events(self.path)
        self.assertEqual(len(events), 2)
        done = events[-1]
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["notify"]["sent"], True)
        self.assertEqual(len(done["errors"][0]), 200)  # truncated

    def test_last_event_filters_by_job(self) -> None:
        ledger = RunLedger(self.path)
        ledger.record(job="fund-run", event="start")
        ledger.record(job="stock-run", event="start")
        self.assertEqual(last_event(self.path, job="stock-run")["job"], "stock-run")
        self.assertIsNone(last_event(self.path, job="backup"))

    def test_corrupt_lines_are_skipped(self) -> None:
        self.path.write_text(
            '{"ts":"2026-10-28T09:00:00+08:00","job":"fund-run","event":"start"}\n'
            "{{{not json}}}\n",
            encoding="utf-8",
        )
        events = load_events(self.path)
        self.assertEqual(len(events), 1)

    def test_events_for_day_and_prune(self) -> None:
        ledger = RunLedger(self.path)
        ledger.record(job="fund-run", event="start", notes="2026-10-28")
        for _ in range(12):
            ledger.record(job="fund-run", event="done", status="ok")
        removed = prune(self.path, keep=5)
        self.assertEqual(removed, 8)
        all_events = load_events(self.path)
        self.assertEqual(len(all_events), 5)
        self.assertEqual(last_event(self.path)["status"], "ok")
        self.assertEqual(len(events_for_day(self.path, all_events[-1]["ts"][:10])), 5)


if __name__ == "__main__":
    unittest.main()
