#!/usr/bin/env python3
"""Deploy Phase 1 launchd jobs: backup (18:45) + watchdog (19:20/20:20/20:50).

Idempotent: rerunning replaces the same labels. Existing wrapper files are
backed up in place before being rewritten. Run from the repo root:
    python3 scripts/install_phase1_launchd.py
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
HOME = Path.home()
UID = 501
REPO_CJK = str(BASE)
PY_BIN = "/Users/t8alex/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12"

WRAPPER_HEADER = f"""#!/bin/zsh
cd "{BASE}" || exit 1
export TZ="Asia/Shanghai"
PYTHON_BIN="{PY_BIN}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "no python3 available"
  exit 1
fi
"""

DEPLOYED_WRAPPERS = {
    "fund": HOME / ".local/share/fund-tracker/run_daily.sh",
    "stock": HOME / ".local/share/stock-tracker/run_daily.sh",
    "model": HOME / ".local/share/model-review/run_monthly.sh",
    "backup": HOME / ".local/share/fund-tracker/run_backup.sh",
    "watchdog": HOME / ".local/share/fund-tracker/run_watchdog.sh",
}


def wrapper_body(kind: str) -> str:
    backup_log = HOME / ".local/share/fund-tracker/backup.log"
    watchdog_log = HOME / ".local/share/fund-tracker/watchdog.log"
    model_log = HOME / ".local/share/model-review/cron.log"
    if kind == "fund":
        return (
            WRAPPER_HEADER
            + '$PYTHON_BIN -m fund_tracker run "$@" >> data/cron.log 2>&1\ncode=$?\n'
            + '$PYTHON_BIN -m shared.run_ledger --path data/run_ledger.jsonl '
            + '--job fund-run --event process --exit-code "$code" >> data/cron.log 2>&1\n'
            + 'exit "$code"\n'
        )
    if kind == "stock":
        return (
            WRAPPER_HEADER
            + '$PYTHON_BIN -m stock_tracker run "$@" >> stock-data/cron.log 2>&1\ncode=$?\n'
            + '$PYTHON_BIN -m shared.run_ledger --path data/run_ledger.jsonl '
            + '--job stock-run --event process --exit-code "$code" >> stock-data/cron.log 2>&1\n'
            + 'exit "$code"\n'
        )
    if kind == "model":
        return (
            WRAPPER_HEADER
            + f'$PYTHON_BIN -m fund_tracker.model_review --if-due --force "$@" >> "{model_log}" 2>&1\ncode=$?\n'
            + '$PYTHON_BIN -m shared.run_ledger --path data/run_ledger.jsonl '
            + f'--job model-review --event process --exit-code "$code" >> "{model_log}" 2>&1\n'
            + 'exit "$code"\n'
        )
    if kind == "backup":
        return (
            WRAPPER_HEADER
            + f'$PYTHON_BIN scripts/backup.py >> "{backup_log}" 2>&1\nexit "$?"\n'
        )
    if kind == "watchdog":
        return (
            WRAPPER_HEADER
            + f'$PYTHON_BIN scripts/watchdog.py >> "{watchdog_log}" 2>&1\nexit "$?"\n'
        )
    raise ValueError(kind)


def deploy_wrapper(kind: str) -> Path:
    path = DEPLOYED_WRAPPERS[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".pre-phase1")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o700)
        if "-m shared.run_ledger " in path.read_text(encoding="utf-8", errors="replace"):
            # already Phase 1 style; still ensure TZ
            content = path.read_text(encoding="utf-8")
            if 'TZ="Asia/Shanghai"' not in content:
                lines = content.splitlines(keepends=True)
                lines.insert(1, 'export TZ="Asia/Shanghai"\n')
                path.write_text("".join(lines), encoding="utf-8")
                path.chmod(0o700)
            return path
    path.write_text(wrapper_body(kind), encoding="utf-8")
    path.chmod(0o700)
    return path


def calendar_entries(entries_specs: list[tuple[int, int]]) -> list[dict]:
    result = []
    for weekday in range(1, 6):
        for hour, minute in entries_specs:
            result.append({"Weekday": weekday, "Hour": hour, "Minute": minute})
    return result


def write_plist(label: str, wrapper: Path, entries: list[tuple[int, int]], log: Path) -> Path:
    plist_path = HOME / "Library/LaunchAgents" / f"{label}.plist"
    data = {
        "Label": label,
        "ProgramArguments": [str(wrapper)],
        "WorkingDirectory": str(HOME / ".local/share/fund-tracker"),
        "StandardOutPath": str(log),
        "StandardErrPath": str(log),
        "EnvironmentVariables": {"TZ": "Asia/Shanghai"},
        "StartCalendarInterval": calendar_entries(entries),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(data, handle)
    return plist_path


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def load_plist(plist_path: Path, label: str) -> str:
    bootout = launchctl("bootout", f"gui/{UID}/{label}")
    bootstrap = launchctl("bootstrap", f"gui/{UID}", str(plist_path))
    if bootstrap.returncode == 0:
        return "loaded"
    detail = (bootstrap.stderr or bootstrap.stdout).strip()
    return f"loaded(bootout={bootout.returncode}, bootstrap={detail or 'ok'})"


def main() -> int:
    process = subprocess.run(
        ["id", "-u"], capture_output=True, text=True, timeout=10
    )
    uid = int(process.stdout.strip()) if process.returncode == 0 else 501
    global UID
    UID = uid

    deployed = {kind: deploy_wrapper(kind) for kind in DEPLOYED_WRAPPERS}
    for kind, path in deployed.items():
        print(f"wrapper[{kind}] {path}")

    backup_plist = write_plist(
        "com.t8alex.fundbackup",
        deployed["backup"],
        [(18, 45)],
        HOME / ".local/share/fund-tracker/launchd.backup.log",
    )
    watchdog_plist = write_plist(
        "com.t8alex.runwatch",
        deployed["watchdog"],
        [(19, 20), (20, 20), (20, 50)],
        HOME / ".local/share/fund-tracker/launchd.watchdog.log",
    )
    print(f"plist 备份任务 {backup_plist}")
    print(f"plist 监控任务 {watchdog_plist}")
    for label, plist_path in (
        ("com.t8alex.fundbackup", backup_plist),
        ("com.t8alex.runwatch", watchdog_plist),
    ):
        state = load_plist(plist_path, label)
        print(f"launchctl[{label}] {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
