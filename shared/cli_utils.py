"""Shared CLI utilities between fund_tracker and stock_tracker."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from shared.trading_calendar import last_weekday_of_month as _last_weekday_of_month

LAUNCHD_LABEL = "com.t8alex.fundtracker"
LAUNCHD_LABEL_STOCK = "com.t8alex.stocktracker"


def _parse_date(value: str | None) -> date:
    """Parse an ISO date string or return today."""
    if not value:
        return date.today()
    return date.fromisoformat(value)


def _fmt(value, digits: int = 2) -> str:
    """Format a number for display, returning '—' for None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a simple ASCII table."""
    if not rows:
        return ""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))
    header = " | ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    sep = "-+-".join("-" * w for w in col_widths)
    lines = [header, sep]
    for row in rows:
        cells = [str(c).ljust(col_widths[i]) for i, c in enumerate(row)]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _summary_period_key(kind: str, value: date) -> str:
    """Return a period key for summary deduplication."""
    if kind == "weekly":
        iso = value.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{value.year}-{value.month:02d}"


def _plist_path() -> Path:
    """Return the launchd plist path."""
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _plist_path_stock() -> Path:
    """Return the stock launchd plist path."""
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL_STOCK}.plist"


def _ensure_launchd_dir() -> Path:
    """Ensure the LaunchAgents directory exists."""
    d = _plist_path().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_launchd_dir_stock() -> Path:
    """Ensure the LaunchAgents directory exists for stock tracker."""
    d = _plist_path_stock().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plist_content(
    config: dict, label: str, module: str, launchd_dir: Path | None = None
) -> str:
    """Generate a launchd plist XML string."""
    plist_path = launchd_dir or Path.home() / "Library/LaunchAgents"
    python_bin = sys.executable
    project_root = Path(__file__).resolve().parent.parent
    schedule = config.get("schedule", {})
    hour = int(schedule.get("hour", 20))
    minute = int(schedule.get("minute", 0))
    data_dir = str(project_root / config.get("data_dir", "data"))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>-m</string>
        <string>{module}</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TZ</key>
        <string>Asia/Shanghai</string>
        <key>PROJECT_ROOT</key>
        <string>{project_root}</string>
        <key>DATA_DIR</key>
        <string>{data_dir}</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{plist_path}/cron.log</string>
    <key>StandardErrorPath</key>
    <string>{plist_path}/cron.err</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""
