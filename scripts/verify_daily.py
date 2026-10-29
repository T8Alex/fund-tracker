"""Read-only production check for a single day's runs.

Usage:
    python3 scripts/verify_daily.py [--date YYYY-MM-DD] [--root .]

Never writes to databases or reports. Exit code 0 when nothing FAILs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

FUND_SOURCES = ("fund_nav", "fund_holdings", "fund_announcements", "macro", "market_index")
SINGLETON_SOURCES = ("macro", "market_index")
FUND_ARTIFACTS = ("market", "macro")
STOCK_ARTIFACTS = ("market", "index_history", "macro")


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def _check(results: list, level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def check_fund(root: Path, day: str, results: list) -> None:
    conn = _connect(root / "data/fund_tracker.db")
    if conn is None:
        _check(results, "FAIL", "基金库", "data/fund_tracker.db 不存在")
        return
    try:
        tables = _tables(conn)
        if "source_health" not in tables:
            _check(results, "FAIL", "source_health", "表不存在")
        else:
            dups = conn.execute(
                "SELECT source, fund_code, COUNT(*) FROM source_health "
                "WHERE run_date=? GROUP BY source, fund_code HAVING COUNT(*)>1",
                (day,),
            ).fetchall()
            _check(
                results,
                "FAIL" if dups else "PASS",
                "source_health 去重",
                f"{len(dups)} 组重复" if dups else "无重复",
            )
            counts = dict(
                conn.execute(
                    "SELECT source, COUNT(*) FROM source_health "
                    "WHERE run_date=? GROUP BY source",
                    (day,),
                ).fetchall()
            )
            for source in SINGLETON_SOURCES:
                value = counts.get(source)
                _check(
                    results,
                    "PASS" if value == 1 else ("WARN" if value is None else "FAIL"),
                    f"{source} 计数",
                    "缺失" if value is None else str(value),
                )
            missing = [s for s in FUND_SOURCES if s not in counts]
            if missing:
                _check(results, "WARN", "source_health 覆盖", "、".join(missing))

        if "run_artifacts" in tables:
            kinds = dict(
                conn.execute(
                    "SELECT kind, COUNT(*) FROM run_artifacts "
                    "WHERE run_date=? GROUP BY kind",
                    (day,),
                ).fetchall()
            )
            for kind in FUND_ARTIFACTS:
                _check(
                    results,
                    "PASS" if kind in kinds else "WARN",
                    f"基金 artifact·{kind}",
                    "已落盘" if kind in kinds else "未落盘（run 后应出现）",
                )
        else:
            _check(results, "WARN", "基金 artifact", "run_artifacts 表尚未创建")

        if "runs" in tables:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_date=?", (day,)
            ).fetchone()
            _check(
                results,
                "PASS" if row else "WARN",
                "基金 runs.status",
                row[0] if row else "当日无记录",
            )
    finally:
        conn.close()

    for name, target in (("latest.md", "latest.md"), ("日报", f"{day}.md")):
        path = root / "reports" / target
        _check(
            results,
            "PASS" if path.exists() else "WARN",
            f"基金报告·{name}",
            "存在" if path.exists() else "缺失",
        )
    latest = root / "reports/latest.md"
    daily = root / f"reports/{day}.md"
    if latest.exists() and daily.exists():
        same = _md5(latest) == _md5(daily)
        _check(
            results,
            "PASS" if same else "FAIL",
            "latest.md 镜像",
            "与当日文件一致" if same else "与当日文件不一致",
        )

    report = daily
    if report.exists():
        text = report.read_text(encoding="utf-8", errors="ignore")
        if "数据完整性" in text:
            row = next(
                (line for line in text.splitlines() if line.startswith("| 大盘指数 |")),
                "",
            )
            ok = "| 1 |" in row
            _check(
                results,
                "PASS" if ok else "WARN",
                "报告数据完整性行",
                row or "未找到",
            )
        else:
            _check(results, "WARN", "报告数据完整性", "报告缺少该小节")


def check_stock(root: Path, day: str, results: list) -> None:
    db_path = root / "stock-data/stock_tracker.db"
    conn = _connect(db_path)
    if conn is None:
        _check(results, "WARN", "股票库", "stock-data/stock_tracker.db 不存在")
        return
    try:
        tables = _tables(conn)
        if "run_artifacts" not in tables:
            _check(results, "WARN", "股票 artifact", "run_artifacts 表尚未创建（17:00 首跑后出现）")
        else:
            kinds = dict(
                conn.execute(
                    "SELECT kind, COUNT(*) FROM run_artifacts "
                    "WHERE run_date=? GROUP BY kind",
                    (day,),
                ).fetchall()
            )
            for kind in STOCK_ARTIFACTS:
                _check(
                    results,
                    "PASS" if kind in kinds else "WARN",
                    f"股票 artifact·{kind}",
                    "已落盘" if kind in kinds else "未落盘",
                )
        if "runs" in tables:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_date=?", (day,)
            ).fetchone()
            _check(
                results,
                "PASS" if row else "WARN",
                "股票 runs.status",
                row[0] if row else "当日无记录",
            )
    finally:
        conn.close()


def check_ledger(root: Path, day: str, results: list) -> None:
    path = root / "data/run_ledger.jsonl"
    if not path.exists():
        _check(results, "WARN", "运行台账", "不存在")
        return
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("ts", ""))[:10] != day:
            continue
        if row.get("event") == "done":
            latest[str(row.get("job"))] = row
    if not latest:
        _check(results, "WARN", "运行台账", "当日无 done 记录")
        return
    for job, row in sorted(latest.items()):
        notify = row.get("notify") or {}
        sent = notify.get("sent")
        webhook = notify.get("webhook")
        level = "PASS"
        detail = f"status={row.get('status')} sent={sent} webhook={webhook}"
        if row.get("status") == "ok" and webhook and not sent:
            level = "WARN"
            detail += "（已配置 webhook 但未发送，可能用了 --no-notify）"
        _check(results, level, f"台账·{job}", detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    results: list[tuple[str, str, str]] = []
    check_fund(root, args.date, results)
    check_stock(root, args.date, results)
    check_ledger(root, args.date, results)

    print(f"验收日期：{args.date}  项目根目录：{root}")
    print()
    width = max(len(name) for _, name, _ in results) if results else 10
    for level, name, detail in results:
        print(f"{level:<4}  {name:<{width}}  {detail}")
    fails = sum(1 for level, _, _ in results if level == "FAIL")
    warns = sum(1 for level, _, _ in results if level == "WARN")
    print()
    print(f"结果：{len(results)} 项检查，FAIL {fails}，WARN {warns}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
