#!/usr/bin/env python3
"""Phase 1 doctor: one-place health check for config/data/jobs/notify/backups.

Usage:
    python3 scripts/doctor.py                # check the real installation
    python3 scripts/doctor.py --root /tmp/restore-dir --skip-launchd
Output is Markdown-ish text; exit code 1 when any FAIL is present.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from shared.run_ledger import load_events  # noqa: E402
from shared.trading_calendar import holidays_coverage, trading_days_between  # noqa: E402

HOME = Path.home()

LAUNCHD_JOBS = {
    "com.t8alex.fundtracker": "基金 20:00 工作日",
    "com.t8alex.stocktracker": "股票 19:00 工作日",
    "com.t8alex.modelreview": "模型复盘 20:30 工作日",
    "com.t8alex.fundbackup": "备份 18:45 工作日",
    "com.t8alex.runwatch": "运行监控 19:20/20:20/20:50 工作日",
}


def _mode_600(path: Path) -> bool:
    try:
        return (path.stat().st_mode & 0o777) == 0o600
    except OSError:
        return False


def db_summary(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        info = {"exists": True, "integrity": integrity, "tables": len(tables)}
        is_fund = path.name == "fund_tracker.db"
        item_table = "funds" if is_fund else "stocks"
        price_table = "nav" if is_fund else "prices"
        info["items"] = connection.execute(f"SELECT COUNT(*) FROM {item_table}").fetchone()[0]
        info["latest_data"] = connection.execute(f"SELECT MAX(date) FROM {price_table}").fetchone()[0]
        info["predictions"] = connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        return info
    finally:
        connection.close()


def check_configs(root: Path) -> tuple[str, str]:
    paths = [root / "config.json", root / "stock-config.json", root / "model-review-config.json"]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        return "FAIL", "缺失配置：" + "、".join(missing)
    bad_mode = [p.name for p in paths if not _mode_600(p)]
    bad_json = []
    webhooks = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            bad_json.append(p.name)
            continue
        url = ((data.get("notify") or {}).get("webhook_url") or "").strip()
        webhooks.append(url)
    if bad_json:
        return "FAIL", "不是合法 JSON：" + "、".join(bad_json)
    if bad_mode:
        return "WARN", "权限非 600：" + "、".join(bad_mode)
    have_webhook = any(url.startswith("https://") for url in webhooks)
    examples_missing = [
        name
        for name in ("config.example.json", "stock-config.example.json", "model-review-config.example.json")
        if not (root / name).exists()
    ]
    detail = "全部存在、权限 600、" + ("webhook 已配置" if have_webhook else "webhook 未配置")
    if examples_missing:
        detail += "；缺示例：" + "、".join(examples_missing)
    return "OK" if have_webhook else "WARN", detail


def check_databases(root: Path) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for label, rel in (
        ("基金库", "data/fund_tracker.db"),
        ("股票库", "stock-data/stock_tracker.db"),
    ):
        path = root / rel
        if not path.exists():
            results.append((label, "FAIL", f"{rel} 不存在"))
            continue
        info = db_summary(path)
        if info.get("integrity") != "ok":
            results.append((label, "FAIL", f"integrity={info.get('integrity')}"))
            continue
        detail = (
            f"integrity ok，{info.get('items')} 只，预测 {info.get('predictions')} 条，"
            f"最新数据 {info.get('latest_data') or '—'}"
        )
        level = "OK"
        latest = str(info.get("latest_data") or "")
        try:
            stale = trading_days_between(date.fromisoformat(latest), date.today())
        except ValueError:
            stale = None
        if stale is None or stale > 5:
            level = "WARN"
            detail += "；超过 5 个交易日无新数据"
        results.append((label, level, detail))
    return results


def check_holdings(root: Path) -> tuple[str, str]:
    path = root / "data/holdings.json"
    if not path.exists():
        return "WARN", "holdings.json 不存在（基金四组和盈亏不可用）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return "FAIL", "holdings.json 不是合法 JSON"
    as_of = str(data.get("as_of") or "")
    try:
        age_days = (date.today() - date.fromisoformat(as_of)).days
    except ValueError:
        age_days = None
    items = data.get("items") or []
    item_sum = round(sum(float(i.get("market_value") or 0) for i in items), 2)
    total = float(data.get("total_value") or 0)
    check_ok = abs(item_sum - total) <= 0.01
    detail = (
        f"as_of={as_of or '—'}，持仓 {len(items)} 只，明细合计 {item_sum}（总 {total}，现金 {data.get('cash', '—')}）"
    )
    if age_days is None:
        return "WARN", detail + "；as_of 无法解析"
    if not check_ok:
        return "FAIL", detail + "；明细合计与 total_value 不一致"
    if age_days > 7:
        return "WARN", detail + f"；已 {age_days} 天未更新（超 7 天阈值）"
    return "OK", detail


def check_ledger(root: Path) -> tuple[str, str]:
    path = root / "data/run_ledger.jsonl"
    if not path.exists():
        return "WARN", "运行台账尚无记录（部署后首次运行前属正常）"
    events = load_events(path)
    if not events:
        return "WARN", "运行台账为空"
    per_job: dict[str, dict] = {}
    for row in events:
        per_job[str(row.get("job"))] = row
    parts = []
    failed_jobs = []
    for job in sorted(per_job):
        last = per_job[job]
        parts.append(f"{job}:{last.get('event')}/{last.get('status') or '—'}@{str(last.get('ts'))[:16]}")
        if last.get("status") == "failed":
            failed_jobs.append(job)
    return ("FAIL" if failed_jobs else "OK"), "；".join(parts)


def check_launchd(uid: int | None = None) -> list[tuple[str, str, str]]:
    uid = uid or os.getuid()
    results = []
    for label, desc in LAUNCHD_JOBS.items():
        completed = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            results.append((label, "FAIL", f"{desc}：未加载"))
            continue
        text = completed.stdout
        state = next(
            (
                line.split("=", 1)[1].strip()
                for line in text.splitlines()
                if line.strip().startswith("state")
            ),
            "?",
        )
        plist_path = HOME / "Library/LaunchAgents" / f"{label}.plist"
        entries = 0
        if plist_path.exists():
            with plist_path.open("rb") as handle:
                data = plistlib.load(handle)
            entries = len(data.get("StartCalendarInterval") or [])
        results.append(
            (
                label,
                "OK" if entries else "WARN",
                f"{desc}：state={state}，{entries} 个触发点",
            )
        )
    return results


def check_webhook_config(root: Path) -> tuple[str, str]:
    filled = []
    for name in ("config.json", "stock-config.json", "model-review-config.json"):
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except ValueError:
            continue
        url = ((data.get("notify") or {}).get("webhook_url") or "").strip()
        filled.append(f"{name}={'已填' if url.startswith('https://') else '未填'}")
    level = "OK" if filled and all("已填" in item for item in filled) else "WARN"
    return level, "；".join(filled) + "（只检查配置，不实际发送）"


def check_model_endpoints(ports: tuple[int, ...] = (11434, 11435, 11436)) -> tuple[str, str]:
    reachable = []
    loaded_any = False
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ps", timeout=3) as response:
                body = json.loads(response.read().decode("utf-8"))
            models = [m.get("name") for m in (body.get("models") or []) if m.get("name")]
            if models:
                loaded_any = True
            reachable.append(f"{port}={'/'.join(models) or '空'}")
        except Exception as exc:  # noqa: BLE001
            reachable.append(f"{port}=不可达({type(exc).__name__})")
    detail = "；".join(reachable)
    if not loaded_any:
        return "WARN", detail + "（模型未驻留属正常；全部端点不可达时 AI 摘要会降级）"
    return "WARN" if "不可达" in str(detail) else "OK", detail


def check_backups() -> tuple[str, str]:
    tier = HOME / ".local/share/fund-tracker/backups/daily"
    if not tier.exists():
        return "WARN", "还没有每日备份目录"
    archives = sorted(p for p in tier.glob("*.tar.enc") if p.is_file())
    if not archives:
        return "WARN", "备份目录为空"
    latest = archives[-1]
    age_hours = (time.time() - latest.stat().st_mtime) / 3600
    detail = f"最新 {latest.name}（{age_hours:.0f} 小时前）"
    if age_hours > 30:
        return "WARN", detail + "；超 30 小时，检查备份任务"
    return "OK", detail


def check_disk() -> tuple[str, str]:
    usage = shutil.disk_usage(BASE)
    gb = usage.free / 1024**3
    level = "FAIL" if gb < 5 else "OK"
    return level, f"剩余 {gb:.0f} GB"


def check_wrappers_tz() -> tuple[str, str]:
    deployed = [
        HOME / ".local/share/fund-tracker/run_daily.sh",
        HOME / ".local/share/stock-tracker/run_daily.sh",
        HOME / ".local/share/model-review/run_monthly.sh",
        HOME / ".local/share/fund-tracker/run_backup.sh",
        HOME / ".local/share/fund-tracker/run_watchdog.sh",
    ]
    missing = [p.name for p in deployed if not p.exists()]
    if missing:
        return "WARN", "缺 wrapper：" + "、".join(missing)
    no_tz = [
        p.name
        for p in deployed
        if 'TZ="Asia/Shanghai"' not in p.read_text(encoding="utf-8", errors="replace")
    ]
    if no_tz:
        return "FAIL", "wrapper 缺 TZ 导出：" + "、".join(no_tz)
    detail = '全部 wrapper 含 TZ="Asia/Shanghai"'
    env_tz = os.environ.get("TZ")
    if env_tz != "Asia/Shanghai":
        detail += "；当前交互终端 TZ 未固定（任务里已由 wrapper 固定），设定 alias 可消除此提醒"
    return "OK", detail


def check_calendar_coverage() -> tuple[str, str]:
    start, end = holidays_coverage()
    if not end:
        return "WARN", "无 A 股节假日数据集（交易日按仅跳周末近似）；运行 scripts/refresh_holidays.py 生成"
    try:
        age_days = (date.today() - date.fromisoformat(end)).days
    except ValueError:
        return "WARN", f"覆盖终点 {end} 无法解析"
    detail = f"交易日集覆盖 {start} … {end}（{age_days} 天前）"
    if age_days > 60:
        return "WARN", detail + "；建议刷新：python3 scripts/refresh_holidays.py"
    return "OK", detail


def collect_checks(root: Path, *, skip_launchd: bool) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    checks.append(("配置", *check_configs(root)))
    for label, level, detail in check_databases(root):
        checks.append((f"数据库·{label}", level, detail))
    checks.append(("持仓文件", *check_holdings(root)))
    checks.append(("运行台账", *check_ledger(root)))
    if skip_launchd:
        checks.append(("launchd", "SKIP", "按要求跳过（恢复演练场景）"))
    else:
        checks.extend((f"launchd·{label}", level, detail) for label, level, detail in check_launchd())
    checks.append(("通知配置", *check_webhook_config(root)))
    checks.append(("模型端点", *check_model_endpoints()))
    checks.append(("备份", *check_backups()))
    checks.append(("磁盘", *check_disk()))
    checks.append(("时区", *check_wrappers_tz()))
    checks.append(("交易日历", *check_calendar_coverage()))
    return checks


ICONS = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭️"}
RESULT_VALUES = ("OK", "WARN", "FAIL", "SKIP")


def run_and_format(root: Path, *, skip_launchd: bool) -> tuple[str, int]:
    checks = collect_checks(root, skip_launchd=skip_launchd)
    counts = {value: 0 for value in RESULT_VALUES}
    for _, level, _ in checks:
        counts[level] += 1
    lines = ["# 基金追踪系统体检（doctor）", ""]
    for name, level, detail in checks:
        lines.append(f"{ICONS.get(level, '•')} [{level}] {name}：{detail}")
    lines.extend(
        [
            "",
            "## 总计",
            "",
            (
                f"- ✅ 正常 {counts['OK']}；⚠️ 提醒 {counts['WARN']}；"
                f"❌ 失败 {counts['FAIL']}；⏭️ 跳过 {counts['SKIP']}"
            ),
        ]
    )
    return "\n".join(lines), (1 if counts["FAIL"] else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="基金追踪系统体检")
    parser.add_argument("--root", default=str(BASE), help="要体检的根目录（恢复演练时指向恢复目录）")
    parser.add_argument("--skip-launchd", action="store_true", help="跳过 launchd 检查（恢复演练用）")
    args = parser.parse_args()
    text, exit_code = run_and_format(Path(args.root), skip_launchd=args.skip_launchd)
    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
