#!/usr/bin/env python3
"""One-off go-live watcher for the 2026-10-26 first scheduled run.

This only reads logs, launchd state and SQLite in read-only mode. It never
writes to the business databases and never sends Feishu messages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
HOME = Path.home()
TZ = ZoneInfo("Asia/Shanghai")
OUT_DIR = HOME / ".local/share/fund-tracker/go-live"
UID = os.getuid()

sys.path.insert(0, str(BASE))
from fund_tracker.notify import macos_notify  # noqa: E402
from fund_tracker.model_review import is_last_weekday  # noqa: E402

JOBS = {
    "stock": {
        "label": "com.t8alex.stocktracker",
        "db": BASE / "stock-data/stock_tracker.db",
        "log": BASE / "stock-data/cron.log",
        "report_dir": BASE / "stock-reports",
        "kind": "股票",
        "expected_runs": 1,
        "daily_prefix": "",
        "table": "stocks",
        "price_table": "prices",
    },
    "fund": {
        "label": "com.t8alex.fundtracker",
        "db": BASE / "data/fund_tracker.db",
        "log": BASE / "data/cron.log",
        "report_dir": BASE / "reports",
        "kind": "基金",
        "expected_runs": 1,
        "daily_prefix": "",
        "table": "funds",
        "price_table": "nav",
    },
    "model": {
        "label": "com.t8alex.modelreview",
        "db": None,
        "log": HOME / ".local/share/model-review/cron.log",
        "report_dir": BASE / "reports",
        "kind": "模型复盘",
        "expected_runs": 2,
        "daily_prefix": "model-review-",
        "table": None,
        "price_table": None,
    },
}


def now() -> datetime:
    return datetime.now(TZ)


def wait_until(hhmm: str) -> None:
    target = datetime.combine(now().date(), dt_time.fromisoformat(hhmm), tzinfo=TZ)
    while now() < target:
        remaining = (target - now()).total_seconds()
        time.sleep(min(60.0, max(1.0, remaining)))


def run_cmd(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def wait_for_start(label: str, expected_runs: int, max_wait: int = 600) -> bool:
    """Wait briefly for launchd to bump its run counter."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        state = launchd_state(label)
        runs = state.get("runs")
        if isinstance(runs, int) and runs >= expected_runs:
            return True
        time.sleep(10)
    return False


def wait_for_idle(label: str, max_wait: int = 1200) -> bool:
    """Wait for a triggered job to leave the running state."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        state = launchd_state(label)
        if state.get("state") != "running":
            return True
        time.sleep(10)
    return False


def launchd_state(label: str) -> dict:
    proc = run_cmd(["launchctl", "print", f"gui/{UID}/{label}"])
    text = proc.stdout if proc.returncode == 0 else ""
    result = {
        "loaded": proc.returncode == 0,
        "state": None,
        "runs": None,
        "last_exit": None,
    }
    for key, field in (
        ("state", "state"),
        ("runs", "runs"),
        ("last exit code", "last_exit"),
    ):
        match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text, re.MULTILINE)
        if match:
            result[field] = match.group(1).strip()
    if result["runs"] is not None:
        try:
            result["runs"] = int(result["runs"])
        except ValueError:
            pass
    return result


def read_tail(path: Path, lines: int = 60) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def query(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db or not db.exists():
        return []
    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def latest_run(db: Path | None) -> dict | None:
    rows = query(
        db,
        "SELECT run_date, status, created_at, summary FROM runs ORDER BY run_date DESC LIMIT 1",
    ) if db else []
    return rows[0] if rows else None


def model_skip_recorded(log_text: str, day: date) -> bool:
    """Return whether today's model-review run explicitly skipped as scheduled."""
    marker = "不是本月最后一个工作日，跳过模型复盘"
    return any(day.isoformat() in line and marker in line for line in log_text.splitlines())


def model_state() -> list[dict]:
    result = []
    for port in (11434, 11436):
        try:
            import requests

            response = requests.get(f"http://127.0.0.1:{port}/api/ps", timeout=2)
            data = response.json() if response.ok else {}
            models = data.get("models", []) if isinstance(data, dict) else []
            result.append(
                {
                    "port": port,
                    "status": response.status_code,
                    "models": [
                        {"name": item.get("name"), "size": item.get("size")}
                        for item in models
                    ],
                }
            )
        except Exception as exc:  # noqa: BLE001
            result.append(
                {
                    "port": port,
                    "status": "unavailable",
                    "models": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return result


def report_info(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def failure_count(text: str, kind: str) -> int | None:
    pattern = re.compile(rf"有\s*(\d+)\s*只{kind}失败")
    matches = pattern.findall(text)
    return int(matches[-1]) if matches else None


def capture(kind: str) -> dict:
    spec = JOBS[kind]
    run_date = now().date()
    today = run_date.isoformat()
    launchd = launchd_state(spec["label"])
    log_text = read_tail(spec["log"])
    run = latest_run(spec["db"])
    if kind == "model":
        report_path = spec["report_dir"] / f"model-review-{today[:7]}.md"
        latest_path = spec["report_dir"] / "model-review-latest.md"
    else:
        report_path = spec["report_dir"] / f"{today}.md"
        latest_path = spec["report_dir"] / "latest.md"
    report = report_info(report_path)
    latest = report_info(latest_path)
    data = {
        "kind": kind,
        "label": spec["label"],
        "captured_at": now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "today": today,
        "launchd": launchd,
        "latest_run": run,
        "report_today": report,
        "report_latest": latest,
        "log_tail": log_text,
        "models": model_state(),
        "verdict": "PASS",
        "warnings": [],
        "failures": [],
    }

    if not launchd["loaded"]:
        data["failures"].append("launchd 任务未加载")
    runs = launchd.get("runs")
    if isinstance(runs, int) and runs < spec["expected_runs"]:
        data["failures"].append(
            f"launchd runs={runs}，低于预期 {spec['expected_runs']}（可能未触发）"
        )
    if kind != "model":
        if not run:
            data["failures"].append("runs 表没有记录")
        else:
            if run.get("run_date") != today:
                data["failures"].append(
                    f"runs 表最新日期是 {run.get('run_date')}，不是今天 {today}"
                )
            if run.get("status") != "ok":
                data["failures"].append(f"最新运行状态是 {run.get('status')}")

    if kind in {"stock", "fund"}:
        if not report["exists"]:
            data["failures"].append("今日日报文件不存在")
        else:
            if report["mtime"][:10] != today:
                data["warnings"].append("日报文件修改时间不是今天")
        count = failure_count(log_text, spec["kind"])
        data["failed_items"] = count
        if count:
            data["warnings"].append(f"日志显示 {count} 只{spec['kind']}失败")

        if spec["db"] and spec["db"].exists():
            data["db_counts"] = {
                "items": query(spec["db"], f"SELECT COUNT(*) AS n FROM {spec['table']}")[0]["n"],
                "predictions": query(spec["db"], "SELECT COUNT(*) AS n FROM predictions")[0]["n"],
                "latest_price": query(
                    spec["db"], f"SELECT MAX(date) AS d FROM {spec['price_table']}"
                )[0]["d"],
            }
    else:
        model_due = is_last_weekday(run_date)
        model_skipped = model_skip_recorded(log_text, run_date)
        data["model_review"] = {
            "due": model_due,
            "skipped": model_skipped,
        }
        if model_due:
            if model_skipped:
                data["failures"].append("模型复盘今天应运行，但日志显示按规则跳过")
            if not report["exists"]:
                data["failures"].append("模型复盘今天应运行，但没有生成月度报告")
            elif report["mtime"][:10] != today:
                data["failures"].append("模型复盘月度报告不是今天生成的")
        else:
            if not model_skipped:
                data["failures"].append("模型复盘未运行，且没有今天的按规则跳过记录")
            if report["exists"] and report["mtime"][:10] == today:
                data["warnings"].append("今天不是月末最后工作日，但生成了新的模型复盘报告")

    if data["failures"]:
        data["verdict"] = "FAIL"
    elif data["warnings"]:
        data["verdict"] = "WARN"
    elif data.get("model_review", {}).get("skipped"):
        data["verdict"] = "SKIP"
    return data


def render(data: dict) -> str:
    run = data.get("latest_run") or {}
    is_model = data["kind"] == "model"
    report_label = "本月模型复盘" if is_model else "今日日报"
    latest_label = "最新模型复盘" if is_model else "最新日报"
    lines = [
        f"# 首跑巡检 · {data['kind']} · {data['captured_at']}",
        "",
        f"- 结论：**{data['verdict']}**",
        f"- launchd：loaded={data['launchd']['loaded']}，"
        f"state={data['launchd']['state']}，runs={data['launchd']['runs']}，"
        f"last_exit={data['launchd']['last_exit']}",
    ]
    if is_model:
        model_review = data.get("model_review", {})
        if model_review.get("skipped"):
            status = "SKIP（不是本月最后一个工作日，正常跳过）"
        elif model_review.get("due"):
            status = "应运行"
        else:
            status = "未按规则跳过"
        lines.append(f"- 执行状态：{status}")
    else:
        lines.append(
            f"- runs 表：{run.get('run_date', '—')} / {run.get('status', '—')} / "
            f"{run.get('created_at', '—')}"
        )
    lines.extend(
        [
            f"- {report_label}：{'存在' if data['report_today']['exists'] else '不存在'} "
            f"{data['report_today'].get('mtime', '')}",
            f"- {latest_label}：{'存在' if data['report_latest']['exists'] else '不存在'} "
            f"{data['report_latest'].get('mtime', '')}",
        ]
    )
    if "db_counts" in data:
        counts = data["db_counts"]
        lines.append(
            f"- 数据库：{counts['items']} 个标的，{counts['predictions']} 条预测，"
            f"最新数据 {counts['latest_price']}"
        )
    if "failed_items" in data:
        lines.append(f"- 失败标的数：{data['failed_items'] if data['failed_items'] is not None else '未解析到'}")
    lines.append(
        "- 本地模型："
        + "；".join(
            f"{item['port']}=" + (",".join(m["name"] or "?" for m in item["models"]) or "空")
            for item in data["models"]
        )
    )
    if data["failures"]:
        lines.append("")
        lines.append("## 失败")
        lines.extend(f"- {item}" for item in data["failures"])
    if data["warnings"]:
        lines.append("")
        lines.append("## 警告")
        lines.extend(f"- {item}" for item in data["warnings"])
    lines.extend(
        [
            "",
            "## 日志尾部",
            "",
            "```text",
            (data["log_tail"] or "(无日志)")[-4000:],
            "```",
            "",
            "> 说明：本地无法确认飞书是否真正送达，请在对应群里目视确认。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=sorted(JOBS), required=True)
    parser.add_argument("--wait-until", help="HH:MM，例如 19:05")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    if args.wait_until:
        wait_until(args.wait_until)
        spec = JOBS[args.kind]
        wait_for_start(spec["label"], spec["expected_runs"])
        wait_for_idle(spec["label"])
    data = capture(args.kind)
    text = render(data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"20261026-{args.kind}.md"
    out_file.write_text(text, encoding="utf-8")
    json_file = OUT_DIR / f"20261026-{args.kind}.json"
    json_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)
    if not args.no_notify:
        if args.kind == "model":
            detail = (
                "正常跳过（不是本月最后一个工作日）"
                if data.get("model_review", {}).get("skipped")
                else f"执行状态={data['verdict']}"
            )
        else:
            detail = f"日报={'有' if data['report_today']['exists'] else '无'}"
        macos_notify(
            f"首跑巡检 · {data['kind']} · {data['verdict']}",
            f"{detail}，runs={data['launchd']['runs']}，详见 {out_file}",
        )
    return 0 if data["verdict"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
