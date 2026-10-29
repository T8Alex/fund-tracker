"""Shared process exit codes so launchd/watchdog can distinguish outcomes."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_STALE = 3

_CODES = {
    "ok": EXIT_OK,
    "partial": EXIT_PARTIAL,
    "stale": EXIT_STALE,
    "failed": EXIT_FAILED,
}


def exit_code_for_status(status: str | None) -> int:
    return _CODES.get(str(status or ""), EXIT_FAILED)
