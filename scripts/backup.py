#!/usr/bin/env python3
"""Backup the fund-tracker system into an encrypted archive.

Usage (from repo root, or via the launchd wrapper):
    python3 scripts/backup.py
    python3 scripts/backup.py --dest ~/.local/share/fund-tracker/backups
 """

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from shared.backup import DEFAULT_DEST, DEFAULT_PASSFILE, run_backup  # noqa: E402

from shared.run_ledger import RunLedger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 DB/配置/持仓/launchd 到加密仓库")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--passfile", default=str(DEFAULT_PASSFILE))
    parser.add_argument("--no-ledger", action="store_true", help="不写运行台账")
    args = parser.parse_args()

    try:
        report = run_backup(BASE, dest=args.dest, passfile=args.passfile)
    except Exception as exc:  # noqa: BLE001
        ledger = RunLedger(BASE / "data/run_ledger.jsonl")
        ledger.record(
            job="backup",
            event="done",
            status="failed",
            notes=str(exc)[:200],
        )
        print(f"备份失败：{exc}")
        return 1

    if not args.no_ledger:
        ledger = RunLedger(BASE / "data/run_ledger.jsonl")
        ledger.record(
            job="backup",
            event="done",
            status="ok" if not report["missing_sources"] else "partial",
            report_path=report["archive"],
            counts={"staged_files": report["staged_files"]},
            notes=",".join(report["tiers"]),
        )
    print(
        f"备份完成：{report['archive']}（{report['staged_files']} 个源文件，"
        f"分档：{'、'.join(report['tiers'])}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
