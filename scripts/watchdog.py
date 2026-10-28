#!/usr/bin/env python3
"""Phase 1 watchdog: alert when a scheduled job silently fails.

Facts come from the run ledger (JSONL), the databases, and the backup dir.
Alerts go to macOS and the fund Feishu bot with retries, then get recorded
in the ledger so the same finding is not spread every 20 minutes.

The pure decision logic lives in evaluate_watch(now, facts) so tests can
drive it with fixed timestamps.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from fund_tracker.notify import macos_notify, webhook_notify  # noqa: E402

from shared.run_ledger import RunLedger, events_for_day  # noqa: E402
from shared.backup import DEFAULT_DEST  # noqa: E402
from shared.trading_calendar import (  # noqa: E402
    is_last_weekday,
    is_trading_day,
    is_weekend,
    trading_days_between,
)

TZ = ZoneInfo("Asia/Shanghai")
LEDGER = BASE / "data/run_ledger.jsonl"

CUTOFFS = {
    "stock": dt_time(17, 12),
    "fund": dt_time(21, 12),
    "model": dt_time(20, 50),
    "backup": dt_time(18, 50),
}
JOB_LABEL = {
    "stock": ("股票任务", "17:00"),
    "fund": ("基金任务", "21:00"),
    "model": ("模型复盘", "20:30"),
    "backup": ("备份任务", "18:45"),
}


def evaluate_watch(now: datetime, facts: dict, *, cutoffs: dict | None = None) -> list[dict]:
    """Return the alert list for this moment. Pure — safe for tests."""
    cutoffs = cutoffs or CUTOFFS
    if is_weekend(now.date()):
        return []
    alerts: list[dict] = []
    for job in ("stock", "fund"):
        if now.time() < cutoffs[job]:
            continue
        state = facts.get(job) or {}
        title, schedule = JOB_LABEL[job]
        if not state.get("has_run_today"):
            # 休市日（法定节假日）不生成新报告是规则内跳过，不算故障
            if is_trading_day(now.date()):
                alerts.append(
                    {
                        "job": job,
                        "severity": "high",
                        "message": (
                            f"{title}（每天 {schedule}）到点没有成功记录："
                            "台账和 runs 表都没有今天的成功运行。"
                            "排查顺序：launchctl print → cron.log → config.json。"
                        ),
                    }
                )
        elif state.get("notify_ok") is False:
            alerts.append(
                {
                    "job": job,
                    "severity": "high",
                    "message": (
                        f"{title}今天运行了，但推送状态是失败。"
                        "请查飞书机器人和网络；报告本身可能没问题。"
                    ),
                }
            )
    model_state = facts.get("model") or {}
    if now.time() >= cutoffs["model"]:
        if is_last_weekday(now.date()) and not model_state.get("run_ok"):
            alerts.append(
                {
                    "job": "model",
                    "severity": "high",
                    "message": "今天应是模型复盘日（本月最后一个工作日），但没有生成复盘报告。",
                }
            )
        elif not is_last_weekday(now.date()) and not model_state.get("run_ok") and not model_state.get("skipped"):
            alerts.append(
                {
                    "job": "model",
                    "severity": "medium",
                    "message": "模型复盘任务今天既没有运行，也没有留下“按规则跳过”的记录。",
                }
            )
    if now.time() >= cutoffs["backup"]:
        backup_state = facts.get("backup") or {}
        latest_ts = backup_state.get("latest_ts")
        if latest_ts is None:
            alerts.append(
                {
                    "job": "backup",
                    "severity": "medium",
                    "message": "还没有任何每日备份归档——备份任务可能没有跑过。",
                }
            )
        else:
            try:
                backup_age = now.timestamp() - datetime.fromisoformat(str(latest_ts)).timestamp()
            except (ValueError, TypeError):
                backup_age = None
            if backup_age is None or backup_age > 30 * 3600:
                alerts.append(
                    {
                        "job": "backup",
                        "severity": "medium",
                        "message": f"最新备份太旧（{latest_ts}，超过 30 小时）——检查备份任务。",
                    }
                )
    data_state = facts.get("data") or {}
    stale_trading_days = data_state.get("stale_trading_days")
    if stale_trading_days is None:
        return alerts
    if stale_trading_days > 3:
        alerts.append(
            {
                "job": "data",
                "severity": "medium",
                "message": (
                    f"最新行情数据是 {data_state.get('latest') or '—'}"
                    f"（{stale_trading_days} 个交易日前）；若今天是交易日，可能是数据源失败。"
                ),
            }
        )
    return alerts


def _has_ok_run_today(db: Path, day: date) -> bool:
    if not db.exists():
        return False
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_date=?", (day.isoformat(),)
            ).fetchone()
        finally:
            connection.close()
        return bool(row) and row[0] in ("ok", "partial")
    except Exception:  # noqa: BLE001
        return False


def _max_date(db: Path, table: str) -> str | None:
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(f"SELECT MAX(date) FROM {table}").fetchone()
        finally:
            connection.close()
        return str(row[0]) if row and row[0] else None
    except Exception:  # noqa: BLE001
        return None


RUN_OK_STATUSES = ("ok", "partial", "skipped")


def collect_facts(now: datetime) -> dict:
    today = now.date()
    facts: dict = {}
    for job, db_rel in (
        ("fund", "data/fund_tracker.db"),
        ("stock", "stock-data/stock_tracker.db"),
    ):
        db = BASE / db_rel
        done_events = [
            row
            for row in events_for_day(LEDGER, today, job)
            if row.get("event") == "done"
            and row.get("status") in RUN_OK_STATUSES
        ]
        done = done_events[-1] if done_events else None
        has_run = bool(done) or _has_ok_run_today(db, today)
        notify = (done or {}).get("notify") if done else None
        if notify is None:
            notify_ok = None
        elif not notify.get("webhook"):
            # webhook 未配置时 pushed 必然失败，不算异常
            notify_ok = None
        else:
            notify_ok = bool(notify.get("sent"))
        facts[job] = {"has_run_today": has_run, "notify_ok": notify_ok}
    model_events = [
        row for row in events_for_day(LEDGER, today, "model-review") if row.get("event") == "done"
    ]
    model_done = model_events[-1] if model_events else None
    facts["model"] = {
        "run_ok": bool(model_done) and model_done.get("status") == "ok",
        "skipped": bool(model_done) and model_done.get("status") == "skipped",
    }
    daily_dir = Path(DEFAULT_DEST) / "daily"
    archives = sorted(p for p in daily_dir.glob("*.tar.enc") if p.is_file()) if daily_dir.exists() else []
    facts["backup"] = {
        "latest_ts": (
            datetime.fromtimestamp(archives[-1].stat().st_mtime, TZ).isoformat(timespec="seconds")
            if archives
            else None
        )
    }
    latest = _max_date(BASE / "data/fund_tracker.db", "nav")
    stale_trading_days = None
    if latest:
        try:
            stale_trading_days = trading_days_between(date.fromisoformat(latest), today)
        except ValueError:
            stale_trading_days = None
    facts["data"] = {"latest": latest, "stale_trading_days": stale_trading_days}
    return facts


def _jobs_alerted_today(path: Path, day: date) -> set[str]:
    """Jobs already alerted to-day, so one problem doesn't beep every cycle."""
    jobs: set[str] = set()
    for row in events_for_day(path, day, "watchdog"):
        if row.get("event") != "alert":
            continue  # alert-failed 投递失败，不抑制下一轮重试
        counts = row.get("counts") or {}
        jobs.update(str(item) for item in counts.get("jobs", []))
    return jobs


def _send_alert(message: str, title: str, *, no_macos: bool, cfg: dict) -> bool:
    ok = False
    if not no_macos:
        ok = macos_notify(title, message) or ok
    if cfg["webhook_url"]:
        ok = (
            webhook_notify(
                cfg["webhook_url"],
                title,
                message,
                secret=cfg["secret"],
            )
            or ok
        )
    return ok


def _record_alert(message: str, *, ok: bool, jobs: list[str], no_ledger: bool) -> None:
    if no_ledger:
        return
    RunLedger(LEDGER).record(
        job="watchdog",
        event="alert" if ok else "alert-failed",
        status="ok" if ok else "failed",
        notes=message[:200],
        counts={"alerts": len(jobs) or 1, "jobs": jobs},
    )


def _test_alert(args, cfg: dict) -> int:
    message = "这是运行监控的送达测试，无需处理。"
    ok = _send_alert(message, "【运行监控】送达演练", no_macos=args.no_macos, cfg=config)
    _record_alert("演练：" + message, ok=ok, jobs=[], no_ledger=args.no_ledger)
    print("演练报警已发送。" if ok else "演练报警发送失败（macOS 与飞书均失败）。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchdog", description="到点没跑/没推送就报警")
    parser.add_argument("--dry-run", action="store_true", help="只打印命中结果，不发送通知")
    parser.add_argument("--no-macos", action="store_true", help="只发飞书，不弹 macOS 通知")
    parser.add_argument("--no-ledger", action="store_true", help="不写台账事件")
    parser.add_argument(
        "--test-alert", action="store_true", help="演练：走完整送达链路，发一条无需处理的测试报警"
    )
    args = parser.parse_args(argv)

    if args.test_alert:
        return _test_alert(args, load_alert_config())

    now = datetime.now().astimezone(TZ)
    facts = collect_facts(now)
    alerts = evaluate_watch(now, facts)
    expected_jobs = {str(alert.get("job")) for alert in alerts}
    if not args.dry_run and not args.no_ledger:
        done_jobs = _jobs_alerted_today(LEDGER, now.date())
        alerts = [a for a in alerts if a.get("job") not in done_jobs]

    if args.dry_run:
        print(
            json.dumps(
                {"now": now.isoformat(), "facts": facts, "alerts": alerts},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not alerts:
        if expected_jobs:
            print(
                f"{now:%Y-%m-%d %H:%M}：{'、'.join(sorted(expected_jobs))} 今天已报过警，不重复。"
            )
            return 0
        print(f"{now:%Y-%m-%d %H:%M}：检查通过，无需报警。")
        return 0

    config = load_alert_config()
    message = "\n".join(row.get("message", "") for row in alerts)
    ok = _send_alert(
        message, "【运行监控】到点没跑或没推送", no_macos=args.no_macos, cfg=config
    )
    if not args.no_ledger:
        RunLedger(LEDGER).record(
            job="watchdog",
            event="alert" if ok else "alert-failed",
            status="ok" if ok else "failed",
            notes=message[:200],
            counts={"alerts": len(alerts), "jobs": sorted(expected_jobs)},
        )
    print(message)
    if not ok:
        print("报警发送失败（macOS 与飞书均失败）；已记入台账，可看 launchd 日志。")
    return 0 if ok else 1


def load_alert_config() -> dict:
    config_path = BASE / "config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    notify = data.get("notify") or {}
    return {
        "webhook_url": (notify.get("webhook_url") or "").strip(),
        "secret": (
            notify.get("webhook_secret") or notify.get("feishu_secret") or ""
        ).strip(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
