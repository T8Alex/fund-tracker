"""Run ledger: one JSON line per notable job event.

This is the operational evidence store ("did the 20:00 job run yesterday, and
did the push actually go out?") — it must not depend on cron.log parsing.

Events are append-only JSON with a small stable schema. Corrupt/partial lines
are skipped when reading, never crash the caller.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

OPTIONAL_FIELDS = (
    "status",
    "exit_code",
    "duration_ms",
    "report_path",
    "notify",
    "counts",
    "errors",
    "data_date",
    "notes",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class RunLedger:
    """Append events to a JSONL ledger file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(
        self,
        *,
        job: str,
        event: str,
        status: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        report_path: str | None = None,
        notify: dict | None = None,
        counts: dict | None = None,
        errors: list[str] | None = None,
        data_date: str | None = None,
        notes: str | None = None,
        **extra: Any,
    ) -> dict:
        row: dict[str, Any] = {
            "ts": now_iso(),
            "job": job,
            "event": event,
        }
        values = {
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "report_path": report_path,
            "notify": notify,
            "counts": counts,
            "errors": (
                [str(item)[:200] for item in errors[:10]] if errors else None
            ),
            "data_date": data_date,
            "notes": notes,
        }
        for key, value in values.items():
            if value is not None:
                row[key] = value
        for key, value in extra.items():
            row[key] = value
        self.append_row(row)
        return row

    def append_row(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_events(path: str | Path) -> list[dict]:
    """Read all parseable events, newest last. Malformed lines are skipped."""
    path = Path(path)
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def last_event(
    path: str | Path,
    job: str | None = None,
    event: str | None = None,
) -> dict | None:
    filtered = [
        row
        for row in load_events(path)
        if (job is None or row.get("job") == job)
        and (event is None or row.get("event") == event)
    ]
    return filtered[-1] if filtered else None


def events_for_day(
    path: str | Path,
    day: date | str,
    job: str | None = None,
) -> list[dict]:
    if isinstance(day, str):
        day = date.fromisoformat(day)
    prefix = day.isoformat()
    return [
        row
        for row in load_events(path)
        if str(row.get("ts", "")).startswith(prefix)
        and (job is None or row.get("job") == job)
    ]


def prune(path: str | Path, keep: int = 5000) -> int:
    """Keep only the newest `keep` events. Returns removed count."""
    path = Path(path)
    events = load_events(path)
    if len(events) <= keep:
        return 0
    removed = len(events) - keep
    lines = [
        json.dumps(row, ensure_ascii=False) for row in events[-keep:]
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return removed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m shared.run_ledger",
        description="向运行台账追加一条事件（供 wrapper / 人工调用）。",
    )
    parser.add_argument("--path", required=True, help="台账 JSONL 路径")
    parser.add_argument("--job", required=True, help="任务名，例如 fund-run")
    parser.add_argument("--event", default="process", help="事件类型，默认 process")
    parser.add_argument("--status", help="ok / partial / failed / skipped")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--notes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ledger = RunLedger(args.path)
    ledger.record(
        job=args.job,
        event=args.event,
        status=args.status,
        exit_code=args.exit_code,
        notes=args.notes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
