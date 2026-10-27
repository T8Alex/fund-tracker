from __future__ import annotations

import argparse
import json
import sqlite3
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from fund_tracker.config import BASE_DIR
from fund_tracker.learning import (
    HORIZONS,
    evaluate_predictions,
    summarize_predictions,
)
from fund_tracker.notify import notify_report
from fund_tracker.store import Database as FundDatabase
from stock_tracker.store import Database as StockDatabase

CONFIG_PATH = BASE_DIR / "model-review-config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "report_dir": "reports",
    "fund_db": "data/fund_tracker.db",
    "stock_db": "stock-data/stock_tracker.db",
    "min_samples": 20,
    "schedule": {"hour": 20, "minute": 30},
    "notify": {
        "macos": True,
        "webhook_url": "",
        "webhook_secret": "",
    },
}

GROUP_ORDER = ("继续持有", "可以跟进持有", "可以调整", "可以出货", "暂不动作")
STATUS_EVALUATED = {"hit", "miss", "flat", "neutral"}


def _merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULT_CONFIG)
    return _merge(DEFAULT_CONFIG, raw)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def last_weekday_of_month(day: date) -> date:
    if day.month == 12:
        first_next = date(day.year + 1, 1, 1)
    else:
        first_next = date(day.year, day.month + 1, 1)
    last = first_next - timedelta(days=1)
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return last


def is_last_weekday(day: date) -> bool:
    return day == last_weekday_of_month(day)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.2f}%"


def _fmt_rate(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None else "—"


def _label(item: dict | None) -> str:
    if not item:
        return "—"
    return (
        f"{item.get('name') or item.get('code')}（{item.get('code')}）"
        f"·{item.get('action') or '—'}·{int(item.get('horizon_days') or 0)}日"
    )


def _load_predictions(db_path: Path, as_of: str) -> list[dict]:
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT * FROM predictions
            WHERE report_date<=? AND report_kind='review'
            ORDER BY report_date, id
            """,
            (str(as_of),),
        ).fetchall()
    return [dict(row) for row in rows]


def _stats(rows: list[dict]) -> dict:
    evaluated = [
        row
        for row in rows
        if str(row.get("outcome_status") or "pending") in STATUS_EVALUATED
    ]
    directional = [
        row
        for row in rows
        if str(row.get("outcome_status") or "pending") in {"hit", "miss"}
    ]
    hit = sum(1 for row in rows if row.get("outcome_status") == "hit")
    miss = sum(1 for row in rows if row.get("outcome_status") == "miss")
    flat = sum(1 for row in rows if row.get("outcome_status") == "flat")
    neutral = sum(1 for row in rows if row.get("outcome_status") == "neutral")
    pending = sum(
        1 for row in rows if str(row.get("outcome_status") or "pending") == "pending"
    )
    returns = [
        value
        for row in evaluated
        if (value := _as_float(row.get("outcome_return"))) is not None
    ]
    adverse = [
        value
        for row in evaluated
        if (value := _as_float(row.get("max_adverse"))) is not None
    ]
    return {
        "total": len(rows),
        "evaluated": len(evaluated),
        "directional": len(directional),
        "hit": hit,
        "miss": miss,
        "flat": flat,
        "neutral": neutral,
        "pending": pending,
        "hit_rate": hit / len(directional) * 100.0 if directional else None,
        "avg_return": mean(returns) if returns else None,
        "avg_adverse": mean(adverse) if adverse else None,
    }


def _group_for(row: dict) -> str:
    group = str(row.get("group_name") or "").strip()
    if group in GROUP_ORDER:
        return group
    action = str(row.get("action") or "")
    if any(word in action for word in ("剔除", "减仓", "出货", "止盈")):
        return "可以出货"
    if any(word in action for word in ("再平衡", "复核", "调整")):
        return "可以调整"
    if "观察" in action:
        return "暂不动作"
    if any(word in action for word in ("买入", "试探", "跟进")):
        return "可以跟进持有"
    if "持有" in action:
        return "继续持有"
    return "暂不动作"


def _evidence_bucket(row: dict) -> str:
    value = _as_float(row.get("evidence"))
    if value is None:
        return "无证据分"
    if value >= 80:
        return "80–100"
    if value >= 70:
        return "70–79"
    if value >= 60:
        return "60–69"
    return "0–59"


def _module_data(
    label: str,
    db_path: Path,
    as_of: str,
) -> dict:
    rows = _load_predictions(db_path, as_of)
    summary = summarize_predictions(
        db_path,
        as_of=as_of,
        report_kind="review",
        module_label=label,
    )
    horizons: dict[int, dict] = {}
    for horizon in HORIZONS:
        horizons[horizon] = _stats(
            [row for row in rows if int(row.get("horizon_days") or 0) == horizon]
        )

    groups: dict[str, dict[int, dict]] = {
        group: {} for group in GROUP_ORDER
    }
    for group in GROUP_ORDER:
        group_rows = [row for row in rows if _group_for(row) == group]
        for horizon in HORIZONS:
            groups[group][horizon] = _stats(
                [
                    row
                    for row in group_rows
                    if int(row.get("horizon_days") or 0) == horizon
                ]
            )

    evidence: dict[str, dict[int, dict]] = {}
    for bucket in ("80–100", "70–79", "60–69", "0–59", "无证据分"):
        bucket_rows = [row for row in rows if _evidence_bucket(row) == bucket]
        if not bucket_rows:
            continue
        evidence[bucket] = {}
        for horizon in HORIZONS:
            evidence[bucket][horizon] = _stats(
                [
                    row
                    for row in bucket_rows
                    if int(row.get("horizon_days") or 0) == horizon
                ]
            )

    return {
        "label": label,
        "db_path": str(db_path),
        "total": len(rows),
        "pending": sum(
            1
            for row in rows
            if str(row.get("outcome_status") or "pending") == "pending"
        ),
        "horizons": horizons,
        "groups": groups,
        "evidence": evidence,
        "summary": summary,
    }


def _empty_module(label: str) -> dict:
    horizons = {horizon: _stats([]) for horizon in HORIZONS}
    return {
        "label": label,
        "db_path": "",
        "total": 0,
        "pending": 0,
        "horizons": horizons,
        "groups": {group: {h: _stats([]) for h in HORIZONS} for group in GROUP_ORDER},
        "evidence": {},
        "summary": {},
    }


def _evaluate_module(
    label: str,
    db_path: Path,
    as_of: str,
    *,
    stock: bool,
) -> dict:
    if not db_path.exists():
        return _empty_module(label)
    try:
        if stock:
            database = StockDatabase(db_path)
            database.init()
            evaluate_predictions(
                db_path,
                as_of,
                lambda code, start, end: [
                    {"date": row.get("date"), "close": row.get("close")}
                    for row in database.get_price_range(code, start, end)
                ],
            )
        else:
            database = FundDatabase(db_path)
            database.init()
            evaluate_predictions(
                db_path,
                as_of,
                lambda code, start, end: [
                    {"date": row.get("date"), "nav": row.get("nav")}
                    for row in database.get_nav_range(code, start, end)
                ],
            )
        return _module_data(label, db_path, as_of)
    except Exception as exc:  # noqa: BLE001 - review must not die on one module
        empty = _empty_module(label)
        empty["error"] = str(exc)
        return empty


def _horizon_table(module: dict) -> list[str]:
    lines = [
        "| 周期 | 总记录 | 已评估 | 待评估 | 方向样本 | 命中 | 未中 | 方向命中率 | 平均涨跌 | 平均最大不利 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in HORIZONS:
        stats = module["horizons"][horizon]
        lines.append(
            f"| {horizon} 日 | {stats['total']} | {stats['evaluated']} | "
            f"{stats['pending']} | {stats['directional']} | {stats['hit']} | "
            f"{stats['miss']} | {_fmt_rate(stats['hit_rate'])} | "
            f"{_fmt_pct(stats['avg_return'])} | "
            f"{_fmt_pct(stats['avg_adverse'])} |"
        )
    return lines


def _threshold_table(fund: dict, stock: dict, min_samples: int) -> list[str]:
    lines = [
        "| 模块 | 周期 | 方向样本 | 门槛 | 状态 |",
        "|---|---|---:|---:|---|",
    ]
    for module in (fund, stock):
        for horizon in HORIZONS:
            stats = module["horizons"][horizon]
            status = (
                "达到门槛"
                if stats["directional"] >= min_samples
                else "样本不足"
            )
            lines.append(
                f"| {module['label']} | {horizon} 日 | {stats['directional']} | "
                f"{min_samples} | {status} |"
            )
    return lines


def _group_cell(stats: dict) -> str:
    if stats["evaluated"] == 0 and stats["pending"] == 0:
        return "—"
    return (
        f"{stats['directional']} / {_fmt_rate(stats['hit_rate'])} / "
        f"{_fmt_pct(stats['avg_return'])}"
    )


def _group_table(module: dict) -> list[str]:
    lines = [
        "| 组 | 2 日（n/命中率/平均） | 5 日 | 20 日 |",
        "|---|---:|---:|---:|",
    ]
    for group in GROUP_ORDER:
        cells = [_group_cell(module["groups"][group][h]) for h in HORIZONS]
        lines.append(f"| {group} | " + " | ".join(cells) + " |")
    return lines


def _evidence_table(module: dict) -> list[str]:
    if not module.get("evidence"):
        return ["- 暂无已评估的证据分样本。"]
    lines = [
        "| 证据分 | 2 日（n/命中率/平均） | 5 日 | 20 日 |",
        "|---|---:|---:|---:|",
    ]
    for bucket in ("80–100", "70–79", "60–69", "0–59", "无证据分"):
        if bucket not in module["evidence"]:
            continue
        cells = [_group_cell(module["evidence"][bucket][h]) for h in HORIZONS]
        lines.append(f"| {bucket} | " + " | ".join(cells) + " |")
    return lines


def _rule_candidates(fund: dict, stock: dict, min_samples: int) -> list[str]:
    candidates: list[str] = []
    for module in (fund, stock):
        for horizon in HORIZONS:
            stats = module["horizons"][horizon]
            if stats["directional"] < min_samples:
                continue
            if stats["hit_rate"] is not None and stats["hit_rate"] < 45:
                candidates.append(
                    f"{module['label']} {horizon} 日方向命中率 "
                    f"{stats['hit_rate']:.0f}%（n={stats['directional']}）低于 45%，"
                    "建议复核评分/证据阈值。"
                )
            if stats["avg_adverse"] is not None and stats["avg_adverse"] < -8:
                candidates.append(
                    f"{module['label']} {horizon} 日平均最大不利 "
                    f"{stats['avg_adverse']:.2f}%，超过 -8%，"
                    "建议复核仓位和止损条件。"
                )
            sell = module["groups"]["可以出货"][horizon]
            if sell["directional"] >= min_samples and (
                sell["hit_rate"] is not None and sell["hit_rate"] < 50
            ):
                candidates.append(
                    f"{module['label']} {horizon} 日“可以出货”组命中率 "
                    f"{sell['hit_rate']:.0f}%（n={sell['directional']}）低于 50%，"
                    "建议提高卖出触发门槛。"
                )
    if not candidates:
        reached = any(
            module["horizons"][h]["directional"] >= min_samples
            for module in (fund, stock)
            for h in HORIZONS
        )
        if reached:
            return [
                "已有周期达到样本门槛，但本月没有明确规则候选；"
                "维持现有规则，继续观察。"
            ]
        return [
            "所有周期方向样本都少于 20 条，按规则只记录、不调参。",
            "本月维持评分、证据权重、目标权重和买卖阈值不变。",
        ]
    return candidates[:8]


def _best_worst_lines(module: dict) -> list[str]:
    summary = module.get("summary") or {}
    best = summary.get("best")
    worst = summary.get("worst")
    if not best and not worst:
        return [f"- {module['label']}：暂无到期样本。"]
    lines = [f"- {module['label']}："]
    if best:
        lines.append(
            f"  - 目前最好：{_label(best)} "
            f"{_fmt_pct(_as_float(best.get('outcome_return')))}。"
        )
    if worst:
        lines.append(
            f"  - 目前最差：{_label(worst)} "
            f"{_fmt_pct(_as_float(worst.get('outcome_return')))}。"
        )
    return lines


def _render_report(
    as_of: str,
    fund: dict,
    stock: dict,
    min_samples: int,
) -> str:
    month = as_of[:7]
    reached = [
        f"{module['label']} {horizon} 日"
        for module in (fund, stock)
        for horizon in HORIZONS
        if module["horizons"][horizon]["directional"] >= min_samples
    ]
    status_text = (
        "有周期达到调参门槛，但只给候选，需人工确认"
        if reached
        else "所有周期样本不足，本月不调参"
    )
    lines = [
        f"# 模型表现复盘 · {month}",
        "",
        "> 覆盖：基金 + 股票预测日志。",
        f"> 数据截至：**{as_of}**。",
        "> 只做复盘和规则候选；**不自动改目标权重、不自动改评分阈值、不自动下单**。",
        "",
        "## 一、本月一句话",
        "",
        f"- 基金预测 **{fund['total']}** 条，股票预测 **{stock['total']}** 条。",
        f"- 结论：**{status_text}**。",
        f"- 达到门槛的周期：**{'、'.join(reached) if reached else '无'}**。",
        "- 本月是否自动改目标权重/阈值/下单：**否**。",
        "- 2 日、5 日、20 日窗口高度重叠，样本不是独立实验，不能把命中率当胜率承诺。",
        "",
        "## 二、样本总览",
        "",
        "### 基金",
        "",
        *_horizon_table(fund),
        "",
        "### 股票",
        "",
        *_horizon_table(stock),
        "",
        "### 调参门槛",
        "",
        *_threshold_table(fund, stock, min_samples),
        "",
        "## 三、四组表现",
        "",
        "> 表中为“方向样本 / 方向命中率 / 平均涨跌”；观察类不计命中率。",
        "",
        "### 基金",
        "",
        *_group_table(fund),
        "",
        "### 股票",
        "",
        *_group_table(stock),
        "",
        "## 四、证据分分层",
        "",
        "### 基金",
        "",
        *_evidence_table(fund),
        "",
        "### 股票",
        "",
        *_evidence_table(stock),
        "",
        "## 五、我错在哪",
        "",
        *_best_worst_lines(fund),
        *_best_worst_lines(stock),
        "- 复盘顺序：先分清是逻辑错、执行错，还是短期运气；"
        "不要用一次结果直接改规则。",
        "",
        "## 六、规则候选（只建议，不自动执行）",
        "",
    ]
    for candidate in _rule_candidates(fund, stock, min_samples):
        lines.append(f"- {candidate}")
    lines.extend(
        [
            "",
            "调参前置条件：",
            "",
            "1. 单个周期方向样本至少 **20 条**；",
            "2. 至少 **6 个月滚动回测**；",
            "3. 人工确认，并保留旧规则的回滚路径；",
            "4. 不因为单月表现自动改目标权重或下单。",
            "",
            "## 七、下月只做三件事",
            "",
            "1. 继续记录预测，不因为短期好坏停止；",
            "2. 到期后按 2/5/20 日回填，先看最大不利；",
            "3. 只复核达到门槛的周期，没有门槛就不调参。",
            "",
            "## 八、三原则卡（模型复盘版）",
            "",
            "| 原则 | 本月执行 | 下月 |",
            "|---|---|---|",
            "| 出手少 | 复盘不自动加仓/减仓 | 只列候选，不直接改规则 |",
            "| 持有久 | 用 2/5/20 日窗口，不被单日波动带跑 | 继续按周期回填 |",
            "| 复盘快 | 每月最后一个工作日 20:30 自动生成 | 先看样本量，再看命中率 |",
            "",
            "## 九、数据口径",
            "",
            "- 基金按累计净值，股票按收盘价；不含交易成本、税费和分红再投资差异。",
            "- 2/5/20 日按交易日近似，遇到长假会顺延。",
            "- 观察/复核类记录为 neutral，不计入方向命中率。",
            "- AI 新闻/文章摘要没有可回填的买卖动作，暂不计入命中率，只作为人工阅读材料。",
            "- 报告只反映本地预测日志，不代表真实账户收益。",
            "",
            "---",
            "",
            f"_生成时间：{date.today().isoformat()} · 本地工具 model-review v0.1.0_",
            "",
        ]
    )
    return "\n".join(lines)


def _summary_text(
    as_of: str,
    fund: dict,
    stock: dict,
    path: Path,
    min_samples: int = 20,
) -> str:
    reached = any(
        module["horizons"][horizon]["directional"] >= min_samples
        for module in (fund, stock)
        for horizon in HORIZONS
    )
    status = "有周期达到门槛，需人工复核" if reached else "样本不足，本月不调参"
    return (
        f"{as_of[:7]} 模型复盘：基金 {fund['total']} 条、股票 {stock['total']} 条；"
        f"{status}。报告：{path}"
    )


def build_report(
    as_of: str,
    config: dict | None = None,
    *,
    force: bool = False,
) -> tuple[Path, str, str, dict, dict]:
    config = config or load_config()
    min_samples = int(config.get("min_samples", 20) or 20)
    fund_path = resolve_path(config.get("fund_db", "data/fund_tracker.db"))
    stock_path = resolve_path(config.get("stock_db", "stock-data/stock_tracker.db"))
    fund = _evaluate_module(
        "基金",
        fund_path,
        as_of,
        stock=False,
    )
    stock = _evaluate_module(
        "股票",
        stock_path,
        as_of,
        stock=True,
    )
    report = _render_report(as_of, fund, stock, min_samples)
    report_dir = resolve_path(config.get("report_dir", "reports"))
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"model-review-{as_of[:7]}.md"
    if path.exists() and not force:
        # The caller normally checks this; keep the function idempotent.
        existing = path.read_text(encoding="utf-8")
        summary = _summary_text(as_of, fund, stock, path, min_samples)
        return path, summary, existing, fund, stock
    path.write_text(report, encoding="utf-8")
    latest = report_dir / "model-review-latest.md"
    latest.write_text(report, encoding="utf-8")
    return (
        path,
        _summary_text(as_of, fund, stock, path, min_samples),
        report,
        fund,
        stock,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model-review",
        description="每月模型表现复盘：基金 + 股票预测日志。",
    )
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--force", action="store_true", help="同月已生成也强制覆盖")
    parser.add_argument(
        "--if-due",
        action="store_true",
        help="只有今天/指定日期是本月最后一个工作日才运行",
    )
    parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    parser.add_argument("--config", help="配置文件路径，默认 model-review-config.json")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print("日期格式应为 YYYY-MM-DD")
        return 2

    if args.if_due and not is_last_weekday(day):
        print(f"{day.isoformat()} 不是本月最后一个工作日，跳过模型复盘。")
        return 0

    report_dir = resolve_path(config.get("report_dir", "reports"))
    path = report_dir / f"model-review-{day.isoformat()[:7]}.md"
    if path.exists() and not args.force:
        print(f"本月模型复盘已存在，跳过：{path}")
        return 0

    path, summary, report, fund, stock = build_report(
        day.isoformat(),
        config,
        force=args.force,
    )
    print(summary)
    if not args.no_notify:
        notify_report(
            config,
            f"模型表现复盘 · {day.isoformat()[:7]}",
            summary,
            report,
        )
    print(f"报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
