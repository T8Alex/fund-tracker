from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path


from shared.cli_utils import _parse_date, _fmt, _table, _last_weekday_of_month, _summary_period_key
from .ai_digest import (
    build_daily_digest,
    build_weekly_digest,
    render_ai_error,
    render_ai_section,
    render_weekly_ai_section,
)
from .config import BASE_DIR, CONFIG_PATH, ensure_dirs, load_config, save_config
from .fetch import (
    FetchError,
    HttpClient,
    fetch_fund,
    fetch_fund_announcements,
    fetch_fund_holdings,
    fetch_market,
    fetch_market_history,
    fetch_market_history_range,
    fetch_profile,
    fetch_ranking,
    fetch_stock_quotes,
    search_fund,
)
from .evidence import build_evidence
from .freshness import assess as assess_freshness, banner as freshness_banner
from .holdings import (
    append_holdings_snapshot,
    attach_holdings,
    load_holdings,
    load_holdings_ledger,
    snapshot_performance,
)
from .learning import (
    evaluate_predictions,
    record_predictions,
    render_learning_section,
    summarize_predictions,
)
from .macro import build_macro_section, fetch_macro_snapshot
from .metrics import (
    build_metrics,
    classify_category,
    normalize_category,
    parse_ac_worth,
    parse_allocation,
    parse_net_worth,
)
from .notify import (
    is_feishu_webhook,
    macos_notify,
    notify,
    notify_report,
    webhook_notify,
)
from .report import (
    _enrich_recommendations,
    generate_report,
    generate_review,
    generate_summary_report,
    next_month_last_weekday,
    next_trading_date,
    save_report,
    save_review_report,
    save_summary_report,
    summary_text,
    summary_text_review,
    summary_text_summary,
)
from .store import Database

from shared.run_ledger import RunLedger
from shared.trading_calendar import is_holiday_weekday

LOG = logging.getLogger(__name__)
LAUNCHD_LABEL = "com.t8alex.fundtracker"
# launchd does not reliably pass non-ASCII paths on macOS. Keep a small ASCII
# wrapper in a non-protected directory; it cd's into the real project and runs
# Python directly (nesting another zsh script breaks on the Chinese path).
LAUNCHD_DIR = Path.home() / ".local" / "share" / "fund-tracker"


def _db(config: dict) -> Database:
    database = Database(BASE_DIR / config["data_dir"] / "fund_tracker.db")
    database.init()
    return database


def _client(config: dict) -> HttpClient:
    return HttpClient(config)


def _learning_series_loader(database: Database):
    def load(code: str, start_date: str, end_date: str):
        return [
            {"date": row.get("date"), "nav": row.get("nav")}
            for row in database.get_nav_range(code, start_date, end_date)
        ]

    return load


def _holdings_with_snapshot(config: dict, rows: list[dict]) -> tuple[dict, dict | None]:
    """Attach holdings and append a snapshot (idempotent per as_of)."""
    holdings = attach_holdings(rows, load_holdings(config))
    stats = None
    try:
        append_holdings_snapshot(config, holdings)
        stats = snapshot_performance(load_holdings_ledger(config))
    except Exception as exc:  # noqa: BLE001 - snapshot is best-effort
        print(f"持仓快照写入失败：{exc}")
    return holdings, stats


def _learning_context(
    database: Database,
    as_of: str,
    *,
    module: str = "基金",
) -> str:
    try:
        evaluate_predictions(
            database.path,
            as_of,
            _learning_series_loader(database),
        )
        summary = summarize_predictions(
            database.path,
            as_of=as_of,
            report_kind="review",
            module_label=module,
        )
        return render_learning_section(summary, module)
    except Exception as exc:  # noqa: BLE001 - learning must not block the report
        return (
            "## 学习与复盘（自动）\n\n"
            f"_学习数据暂不可用：{exc}_\n"
        )


def _ai_daily_section(
    database: Database,
    config: dict,
    macro_snapshot: dict | None,
    run_date_text: str,
) -> str:
    """B 阶段：只读取本项目新闻/摘要，交给本地模型做事实提炼。

    这里不提供通用聊天入口；模型只服务于项目报告中的新闻/文章分析。
    """
    ai_config = config.get("ai") or {}
    if not ai_config.get("enabled", False):
        return ""
    try:
        result = build_daily_digest(
            db_path=database.path,
            module="基金",
            digest_date=run_date_text,
            news=(macro_snapshot or {}).get("news") or [],
            macro_snapshot=macro_snapshot,
            config=ai_config,
        )
        return render_ai_section(result) if result else ""
    except Exception as exc:  # noqa: BLE001 - AI must not block the report
        print(f"AI 新闻摘要生成失败：{exc}")
        return render_ai_error(exc)


def _ai_weekly_section(
    database: Database,
    config: dict,
    macro_snapshot: dict | None,
    rows: list[dict],
    period_start: str,
    report_date_text: str,
    period_label: str,
) -> str:
    """周度复盘用本地 DeepSeek；失败时退回每日 Qwen 摘要。"""
    ai_config = config.get("ai") or {}
    if not ai_config.get("enabled", False):
        return ""
    try:
        result = build_weekly_digest(
            db_path=database.path,
            module="基金",
            digest_date=report_date_text,
            period_start=period_start,
            period_end=report_date_text,
            period_label=period_label,
            rows=rows,
            news=(macro_snapshot or {}).get("news") or [],
            macro_snapshot=macro_snapshot,
            config=ai_config,
        )
        if result:
            return render_weekly_ai_section(result)
    except Exception as exc:  # noqa: BLE001 - fall back to the daily digest
        print(f"AI 周度复盘生成失败，改用每日摘要：{exc}")
    return _ai_daily_section(database, config, macro_snapshot, report_date_text)


def _record_learning(
    database: Database,
    run_date_text: str,
    rows: list[dict],
) -> None:
    try:
        record_predictions(
            database.path,
            run_date_text,
            "review",
            rows,
            price_key="nav",
            module_label="基金",
        )
    except Exception as exc:  # noqa: BLE001 - a log failure is not a report failure
        print(f"学习记录失败：{exc}")

def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    if not CONFIG_PATH.exists():
        save_config(config)
    database = _db(config)
    print(f"初始化完成：{BASE_DIR}")
    print(f"数据库：{database.path}")
    print(f"配置文件：{CONFIG_PATH}")
    print("下一步：python3 -m fund_tracker add 000032")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    client = _client(config)
    database = _db(config)

    code = str(args.code).strip()
    if not re.fullmatch(r"\d{6}", code):
        results = search_fund(client, code)
        if not results:
            print(f"没有找到与“{code}”匹配的基金。")
            return 1
        if len(results) > 1:
            print("找到多只基金，请用 6 位代码重新添加：")
            for item in results[:15]:
                print(f"  {item['code']}  {item['name']}")
            return 1
        code = results[0]["code"]

    try:
        fund = fetch_fund(client, code)
    except FetchError as exc:
        print(f"添加失败：{exc}")
        return 1

    try:
        profile = fetch_profile(client, code)
    except FetchError:
        profile = {}

    category = normalize_category(args.category) or classify_category(
        fund["name"],
        profile.get("fund_type"),
        parse_allocation(fund.get("allocation")),
    )
    database.upsert_fund(
        code=code,
        name=fund["name"],
        category=category,
        role=args.role,
        target_weight=args.weight,
        note=args.note,
    )
    print(f"已添加：{code} {fund['name']}（{category}）")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    config = load_config()
    database = _db(config)
    if database.remove_fund(args.code):
        print(f"已移除 {args.code}")
        return 0
    print(f"跟踪列表中没有 {args.code}")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config()
    database = _db(config)
    funds = database.list_funds()
    if not funds:
        print("跟踪列表为空。")
        return 0
    rows = []
    for fund in funds:
        rows.append(
            [
                fund["code"],
                fund["name"],
                fund.get("category") or "—",
                fund.get("role") or "—",
                _fmt(fund.get("target_weight"), 2) if fund.get("target_weight") else "—",
                fund.get("note") or "",
            ]
        )
    print(_table(["代码", "名称", "类别", "角色", "目标权重", "备注"], rows))
    print(f"\n共 {len(funds)} 只。")
    return 0


def _parse_inception(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _base_name(name: str) -> str:
    return re.sub(r"(A|B|C|D|E|H|I)$", "", name.strip())


def cmd_discover(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    client = _client(config)
    database = _db(config)

    try:
        records = fetch_ranking(
            client,
            args.type,
            pages=args.pages,
            page_size=args.page_size,
            subtype=args.subtype,
        )
    except FetchError as exc:
        print(f"获取排行榜失败：{exc}")
        return 1

    keywords = [item.strip() for item in (args.keyword or []) if item.strip()]
    today = date.today()
    selected = []
    seen: dict[str, dict] = {}

    for record in records:
        name = record["name"]
        if keywords and not any(keyword in name for keyword in keywords):
            continue
        if not args.include_convertible and ("可转债" in name or "转债" in name):
            continue
        if any(word in name for word in ["定开", "持有期", "封闭", "养老目标"]):
            continue
        if "QDII" in name or "美元" in name or "港币" in name:
            continue
        scale = record.get("scale_yi")
        if scale is None or scale < args.min_size or scale > args.max_size:
            continue
        inception = _parse_inception(record.get("inception"))
        if inception is None:
            continue
        if (today - inception).days < args.min_age * 365:
            continue
        if record.get("year3_pct") is None:
            continue

        base = _base_name(name)
        existing = seen.get(base)
        if existing is None:
            seen[base] = record
        elif existing["name"].endswith("C") and name.endswith("A"):
            seen[base] = record

    selected = sorted(
        seen.values(),
        key=lambda item: item.get("year3_pct") or -999,
        reverse=True,
    )[: args.limit]

    if not selected:
        print("没有符合条件的基金，试试放宽 --min-size / --min-age / --keyword。")
        return 1

    detailed = []
    if args.details or args.add_top:
        for index, record in enumerate(selected, 1):
            print(f"[{index}/{len(selected)}] 分析 {record['code']} {record['name']} ...")
            try:
                fund = fetch_fund(client, record["code"])
                profile = fetch_profile(client, record["code"])
                metrics = build_metrics(fund, profile, config)
                metrics["year3_pct_rank"] = record.get("year3_pct")
                category = metrics.get("category")
                if category in {"纯债", "短债"} and (metrics.get("max_daily_move_all") or 0) > 5:
                    print("  跳过：历史净值出现异常单日波动，疑似大额赎回/数据异常")
                    continue
                if metrics.get("ann_3y") is None:
                    print("  跳过：3 年净值数据不足")
                    continue
                detail_scale = metrics.get("scale_yi")
                if detail_scale is not None and not (args.min_size <= detail_scale <= args.max_size):
                    print(f"  跳过：最新规模 {detail_scale:.1f} 亿，不在 {args.min_size:g}–{args.max_size:g} 亿范围内")
                    continue
                detailed.append(metrics)
            except Exception as exc:  # noqa: BLE001
                print(f"  跳过：{exc}")
            time.sleep(0.15)
        detailed.sort(key=lambda item: item.get("score") or 0, reverse=True)
        output = detailed
    else:
        output = selected

    headers = ["代码", "名称", "近3年", "近1年", "规模(亿)", "成立", "费率"]
    rows = []
    for item in output:
        if args.details or args.add_top:
            rows.append(
                [
                    item["code"],
                    item["name"],
                    _fmt(item.get("ret_3y")) + "%",
                    _fmt(item.get("ret_1y")) + "%",
                    _fmt(item.get("scale_yi"), 1),
                    item.get("inception") or "—",
                    _fmt(item.get("annual_fee_pct"), 2) + "%",
                ]
            )
        else:
            rows.append(
                [
                    item["code"],
                    item["name"],
                    _fmt(item.get("year3_pct")) + "%",
                    _fmt(item.get("year1_pct")) + "%",
                    _fmt(item.get("scale_yi"), 1),
                    item.get("inception") or "—",
                    _fmt(item.get("buy_fee_pct"), 2) + "%",
                ]
            )
    if args.details or args.add_top:
        headers.extend(["回撤(3Y)", "夏普(3Y)", "评分", "类别"])
        for row, item in zip(rows, output):
            row.extend(
                [
                    _fmt(item.get("dd_3y")) + "%",
                    _fmt(item.get("sharpe_3y")),
                    _fmt(item.get("score"), 1),
                    item.get("category") or "—",
                ]
            )

    print()
    print(_table(headers, rows))
    print(f"\n共筛选出 {len(output)} 只；排行榜原始记录 {len(records)} 条。")
    if args.details or args.add_top:
        print("评分是跟踪排序工具，不是买入信号。")

    if args.add_top:
        added = 0
        for item in output[: args.add_top]:
            database.upsert_fund(
                code=item["code"],
                name=item["name"],
                category=normalize_category(args.category) or item.get("category"),
                role=args.role,
                target_weight=None,
                note="discover 自动加入",
            )
            added += 1
            print(f"已加入跟踪：{item['code']} {item['name']}")
        print(f"共加入 {added} 只。")

    return 0


def _build_alerts(metrics: dict, previous: dict | None, config: dict) -> list[dict]:
    alert_config = config.get("alerts", {})
    code = metrics.get("code", "")
    name = metrics.get("name", "")
    alerts: list[dict] = []

    def add(level: str, message: str) -> None:
        alerts.append({"code": code, "name": name, "level": level, "message": message})

    stale_days = int(config.get("stale_days", 5))
    if metrics.get("stale_days", 0) > stale_days:
        add("info", f"净值已 {metrics['stale_days']} 天未更新（最新 {metrics.get('nav_date')}）")

    daily = metrics.get("daily_pct")
    if daily is not None and abs(daily) >= alert_config.get("daily_move_pct", 2.0):
        add("info", f"单日涨跌 {daily:+.2f}%，超过 {alert_config.get('daily_move_pct')}% 观察阈值")

    dd_1y = metrics.get("dd_1y")
    if dd_1y is not None and abs(dd_1y) >= alert_config.get("dd_1y_pct", 8.0):
        add("high", f"近 1 年最大回撤 {dd_1y:.2f}%，超过 {alert_config.get('dd_1y_pct')}% 阈值")

    dd_3y = metrics.get("dd_3y")
    if dd_3y is not None and abs(dd_3y) >= alert_config.get("dd_3y_pct", 15.0):
        add("high", f"近 3 年最大回撤 {dd_3y:.2f}%，超过 {alert_config.get('dd_3y_pct')}% 阈值")

    scale = metrics.get("scale_yi")
    if scale is not None and scale < alert_config.get("size_min_yi", 2.0):
        add("high", f"规模 {scale:.1f} 亿，低于 {alert_config.get('size_min_yi')} 亿清盘风险线")
    if scale is not None and scale > alert_config.get("size_max_yi", 200.0):
        add("info", f"规模 {scale:.1f} 亿，超过 {alert_config.get('size_max_yi')} 亿，调仓灵活性下降")

    category = metrics.get("category")
    max_move = metrics.get("max_daily_move_1y")
    if (
        category in {"纯债", "短债"}
        and max_move is not None
        and max_move >= alert_config.get("bond_daily_move_suspicious", 5.0)
    ):
        add("warn", f"近 1 年单日最大波动 {max_move:.2f}%，纯债基金出现异常波动，核对公告/大额赎回")

    if previous:
        previous_score = previous.get("score")
        current_score = metrics.get("score")
        if previous_score is not None and current_score is not None:
            delta = current_score - previous_score
            metrics["previous_score"] = previous_score
            metrics["score_delta"] = round(delta, 1)
            if delta <= -alert_config.get("score_drop", 8.0):
                add("warn", f"评分较上次下降 {delta:.1f} 分（{previous_score:.1f} → {current_score:.1f}）")

        previous_manager = (previous.get("manager") or {}).get("name")
        current_manager = (metrics.get("manager") or {}).get("name")
        if previous_manager and current_manager and previous_manager != current_manager:
            metrics["manager_changed"] = True
            if alert_config.get("manager_change", True):
                add("high", f"基金经理变更：{previous_manager} → {current_manager}")

        previous_scale = previous.get("scale_yi")
        if previous_scale is not None and scale is not None and previous_scale > 0:
            change = (scale / previous_scale - 1) * 100
            metrics["scale_change_text"] = f"{previous_scale:.1f} → {scale:.1f} 亿（{change:+.1f}%）"
            if abs(change) >= 50:
                add("warn", f"规模较上次变化 {change:+.1f}%（{previous_scale:.1f} → {scale:.1f} 亿）")

        previous_rank = previous.get("rank_pct")
        current_rank = metrics.get("rank_pct")
        if previous_rank is not None and current_rank is not None:
            worse = current_rank - previous_rank
            if worse >= alert_config.get("rank_pct_worse", 50.0):
                add("info", f"同类排名百分位从 {previous_rank:.0f}% 降到 {current_rank:.0f}%")

    return alerts


def _select_announcements(items: list[dict], limit: int = 20) -> list[dict]:
    selected = list(items[:12])
    for keyword in ("年度报告", "中期报告", "季度报告"):
        for item in items:
            title = str(item.get("title") or "")
            if keyword in title and "提示性公告" not in title:
                if item not in selected:
                    selected.append(item)
                break
    return selected[:limit]


def _announcement_alerts(metrics: dict) -> list[dict]:
    code = metrics.get("code", "")
    name = metrics.get("name", "")
    alerts: list[dict] = []
    today = date.today()
    high_keywords = (
        "基金经理变更",
        "清盘",
        "清算",
        "基金合同终止",
        "风险提示",
    )
    warn_keywords = ("暂停申购", "限购")
    skip_keywords = (
        "假期",
        "节假日",
        "非港股通",
        "销售业务",
        "销售机构",
        "提示性公告",
    )
    for announcement in (metrics.get("announcements") or [])[:12]:
        title = str(announcement.get("title") or "")
        date_text = str(announcement.get("date") or "")
        if any(keyword in title for keyword in skip_keywords):
            continue
        try:
            published = date.fromisoformat(date_text[:10])
        except ValueError:
            published = None
        if published is not None and (today - published).days > 45:
            continue
        if any(keyword in title for keyword in high_keywords):
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "level": "high",
                    "message": f"公告：{title}（{date_text or '日期未知'}）",
                }
            )
            break
        if any(keyword in title for keyword in warn_keywords):
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "level": "warn",
                    "message": f"公告：{title}（{date_text or '日期未知'}）",
                }
            )
            break
    return alerts


def _full_report_mode(config: dict) -> str:
    mode = str(config.get("notify", {}).get("full_report", "review")).lower()
    return mode if mode in {"none", "review", "all"} else "review"


def _review_label(config: dict) -> str:
    interval = int(config.get("review", {}).get("interval_trading_days", 2))
    return f"{interval}日推荐"


def _review_due_info(
    database: Database,
    run_date_text: str,
    config: dict,
) -> tuple[bool, str, int, int, dict | None]:
    review_config = config.get("review", {})
    interval = int(review_config.get("interval_trading_days", 2))
    last_review = database.get_last_review()
    if last_review:
        period_start = last_review.get("review_date") or last_review.get("period_end")
        days = database.count_trading_days_since(period_start, run_date_text)
        return days >= interval, period_start, days, interval, last_review
    period_start = (
        date.fromisoformat(run_date_text) - timedelta(days=7)
    ).isoformat()
    return True, period_start, interval, interval, None


def _build_review_rows(
    database: Database,
    run_date_text: str,
    period_start: str,
    window_days: int = 2,
) -> list[dict]:
    rows: list[dict] = []
    for fund in database.list_funds():
        code = fund["code"]
        current = database.get_snapshot_on_or_before(code, run_date_text)
        if not current:
            continue
        row = dict(current)
        if fund.get("role"):
            row["role"] = fund["role"]
        if fund.get("target_weight") is not None:
            row["target_weight"] = fund["target_weight"]
        if fund.get("category"):
            row["category"] = fund["category"]

        window = database.get_nav_window(
            code,
            run_date_text,
            max(2, window_days + 1),
        )
        if len(window) >= 2 and window[0]["nav"] > 0:
            ret = round(
                (window[-1]["nav"] / window[0]["nav"] - 1) * 100,
                2,
            )
            row["ret_window"] = ret
            row["ret_5d"] = ret
            row["period_days"] = window_days
            row["period_label"] = f"{window_days} 日"
            row["nav_start_date"] = window[0]["date"]

        previous = None
        if period_start < run_date_text:
            previous = database.get_snapshot_on_or_before(code, period_start)
        if (
            previous
            and previous.get("score") is not None
            and row.get("score") is not None
        ):
            delta = round(row["score"] - previous["score"], 1)
            row["score_delta_window"] = delta
            row["score_delta_5d"] = delta
        rows.append(row)
    return rows

def _build_period_rows(
    database: Database,
    report_date_text: str,
    period_start: str,
    return_start: str,
    period_label: str,
) -> list[dict]:
    rows: list[dict] = []
    for fund in database.list_funds():
        code = fund["code"]
        current = database.get_snapshot_on_or_before(code, report_date_text)
        if not current:
            continue
        row = dict(current)
        if fund.get("role"):
            row["role"] = fund["role"]
        if fund.get("target_weight") is not None:
            row["target_weight"] = fund["target_weight"]
        if fund.get("category"):
            row["category"] = fund["category"]

        navs = database.get_nav_range(code, return_start, report_date_text)
        if len(navs) >= 2 and navs[0]["nav"] > 0:
            ret = round((navs[-1]["nav"] / navs[0]["nav"] - 1) * 100, 2)
            row["ret_window"] = ret
            row["ret_5d"] = ret
            row["period_days"] = len(navs) - 1
            row["period_label"] = period_label
            row["nav_start_date"] = navs[0]["date"]

        previous = None
        if period_start < report_date_text:
            previous = database.get_snapshot_on_or_before(code, period_start)
        if (
            previous
            and previous.get("score") is not None
            and row.get("score") is not None
        ):
            delta = round(row["score"] - previous["score"], 1)
            row["score_delta_window"] = delta
            row["score_delta_5d"] = delta
        rows.append(row)
    return rows


def _generate_summary_if_due(
    kind: str,
    database: Database,
    config: dict,
    client: HttpClient,
    report_date_text: str,
    *,
    force: bool = False,
    macro_snapshot: dict | None = None,
    macro_section: str | None = None,
) -> tuple[Path, str, list[dict]] | None:
    summary_config = config.get("summary", {})
    if not force:
        if not summary_config.get("enabled", True):
            return None
        if kind == "weekly" and not summary_config.get("weekly_enabled", True):
            return None
        if kind == "monthly" and not summary_config.get("monthly_enabled", True):
            return None

    report_date = date.fromisoformat(report_date_text)
    if kind == "weekly":
        if report_date.weekday() != 4 and not force:
            return None
        iso = report_date.isocalendar()
        period_key = f"{iso.year}-W{iso.week:02d}"
        period_start = (report_date - timedelta(days=7)).isoformat()
        return_start = period_start
        period_label = "本周"
        next_date = (report_date + timedelta(days=7)).isoformat()
    elif kind == "monthly":
        if _last_weekday_of_month(report_date) != report_date and not force:
            return None
        period_key = f"{report_date.year}-{report_date.month:02d}"
        first_current = date(report_date.year, report_date.month, 1)
        period_start = (first_current - timedelta(days=1)).isoformat()
        return_start = first_current.isoformat()
        period_label = "本月"
        next_date = next_month_last_weekday(report_date_text)
    else:
        raise ValueError(f"未知总结类型：{kind}")

    if not force and database.get_summary_report(kind, period_key):
        return None

    rows = _build_period_rows(
        database,
        report_date_text,
        period_start,
        return_start,
        period_label,
    )
    if not rows:
        return None
    start_dates = [
        row.get("nav_start_date") for row in rows if row.get("nav_start_date")
    ]
    report_start = (
        Counter(start_dates).most_common(1)[0][0] if start_dates else return_start
    )
    holdings = attach_holdings(rows, load_holdings(config))

    alerts = database.get_alerts(report_date_text)
    market = fetch_market_history_range(
        client,
        return_start,
        report_date_text,
    )
    if macro_snapshot is None:
        try:
            macro_snapshot = fetch_macro_snapshot(
                client,
                news_limit=int(config.get("macro", {}).get("news_limit", 8)),
                include_news=bool(
                    config.get("macro", {}).get("news_enabled", True)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"宏观数据获取失败，{period_label}总结继续：{exc}")
            macro_snapshot = {}
    if macro_section is None:
        macro_section = (
            build_macro_section(macro_snapshot, config) if macro_snapshot else ""
        )
    alerts_by_code: dict[str, list[dict]] = {}
    for alert in alerts:
        alerts_by_code.setdefault(alert.get("code", ""), []).append(alert)
    evidence = build_evidence(rows, macro_snapshot, alerts_by_code)
    enriched = _enrich_recommendations(rows, alerts_by_code, evidence)
    if kind == "weekly":
        ai_section = _ai_weekly_section(
            database,
            config,
            macro_snapshot,
            enriched,
            return_start,
            report_date_text,
            period_label,
        )
    else:
        ai_section = _ai_daily_section(
            database,
            config,
            macro_snapshot,
            report_date_text,
        )
    learning_section = _learning_context(database, report_date_text)
    content = generate_summary_report(
        kind,
        report_date_text,
        report_start,
        report_date_text,
        rows,
        market,
        alerts,
        config,
        next_report_date=next_date,
        macro_section=macro_section,
        evidence=evidence,
        learning_section=learning_section,
        ai_section=ai_section,
        holdings=holdings,
    )
    path = save_summary_report(
        BASE_DIR / config["report_dir"],
        kind,
        report_date_text,
        content,
    )
    summary = summary_text_summary(
        kind,
        report_date_text,
        rows,
        path,
        alerts,
        evidence=evidence,
        holdings=holdings,
    )
    database.save_summary_report(
        kind,
        period_key,
        report_date_text,
        report_start,
        report_date_text,
        summary,
        str(path),
    )
    return path, summary, rows


def _generate_review_if_due(
    database: Database,
    config: dict,
    client: HttpClient,
    run_date_text: str,
    *,
    force: bool = False,
    macro_snapshot: dict | None = None,
    macro_section: str | None = None,
) -> tuple[Path, str, list[dict]] | None:
    review_config = config.get("review", {})
    if not review_config.get("enabled", True) and not force:
        return None
    due, period_start, days, interval, _ = _review_due_info(
        database,
        run_date_text,
        config,
    )
    if not due and not force:
        return None
    rows = _build_review_rows(
        database,
        run_date_text,
        period_start,
        window_days=interval,
    )
    if not rows:
        return None
    start_dates = [
        row.get("nav_start_date") for row in rows if row.get("nav_start_date")
    ]
    # Different funds can have one missing NAV day. Use the most common
    # window start so the report period reflects the typical review window.
    report_start = (
        Counter(start_dates).most_common(1)[0][0] if start_dates else period_start
    )
    holdings, snapshot = _holdings_with_snapshot(config, rows)
    data_quality = assess_freshness(rows, holdings, config)

    alerts = database.get_alerts(run_date_text)
    market = fetch_market_history(client, count=interval + 1)
    if macro_snapshot is None:
        try:
            macro_snapshot = fetch_macro_snapshot(
                client,
                news_limit=int(config.get("macro", {}).get("news_limit", 8)),
                include_news=bool(
                    config.get("macro", {}).get("news_enabled", True)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"宏观数据获取失败，复盘继续：{exc}")
            macro_snapshot = {}
    if macro_section is None:
        macro_section = (
            build_macro_section(macro_snapshot, config) if macro_snapshot else ""
        )
    alerts_by_code: dict[str, list[dict]] = {}
    for alert in alerts:
        alerts_by_code.setdefault(alert.get("code", ""), []).append(alert)
    evidence = build_evidence(rows, macro_snapshot, alerts_by_code)
    enriched = _enrich_recommendations(rows, alerts_by_code, evidence)
    _record_learning(database, run_date_text, enriched)
    ai_section = _ai_daily_section(
        database,
        config,
        macro_snapshot,
        run_date_text,
    )
    learning_section = _learning_context(database, run_date_text)
    next_date = next_trading_date(run_date_text, interval)
    content = generate_review(
        run_date_text,
        report_start,
        run_date_text,
        rows,
        market,
        alerts,
        config,
        next_review_date=next_date,
        macro_section=macro_section,
        evidence=evidence,
        window_days=interval,
        learning_section=learning_section,
        ai_section=ai_section,
        holdings=holdings,
        snapshot=snapshot,
        data_quality=data_quality,
    )
    path = save_review_report(
        BASE_DIR / config["report_dir"],
        run_date_text,
        content,
    )
    summary = summary_text_review(
        run_date_text,
        rows,
        path,
        alerts,
        evidence=evidence,
        window_days=interval,
        holdings=holdings,
    )
    database.save_review(
        run_date_text,
        report_start,
        run_date_text,
        summary,
        str(path),
    )
    return path, summary, rows


def cmd_review(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    client = _client(config)
    label = _review_label(config)
    if not config.get("review", {}).get("enabled", True) and not args.force:
        print(f"{label}已在 config.json 里关闭；要生成请加 --force。")
        return 0
    due, period_start, days, interval, _ = _review_due_info(
        database,
        run_date_text,
        config,
    )
    if not due and not args.force:
        print(
            f"还没到推荐日：自 {period_start} 起 {days}/{interval} 个交易日。"
        )
        return 0
    result = _generate_review_if_due(
        database,
        config,
        client,
        run_date_text,
        force=args.force,
    )
    if not result:
        print("没有可用快照，先运行 run。")
        return 1
    path, summary, _ = result
    if not args.no_notify:
        if _full_report_mode(config) in {"review", "all"}:
            content = path.read_text(encoding="utf-8")
            notify_report(config, f"基金{label}", summary, content)
        else:
            notify(config, f"基金{label}", summary)
    print(summary)
    print(f"推荐报告：{path}")
    return 0


def _cmd_summary(kind: str, args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    client = _client(config)
    label = "周度总结" if kind == "weekly" else "月度总结"

    if not config.get("summary", {}).get("enabled", True) and not args.force:
        print(f"{label}已在 config.json 里关闭；要生成请加 --force。")
        return 0
    if kind == "weekly" and run_date.weekday() != 4 and not args.force:
        print("周度总结只在每周五生成；要补生成请加 --force。")
        return 0
    if (
        kind == "monthly"
        and _last_weekday_of_month(run_date) != run_date
        and not args.force
    ):
        print("月度总结只在每月最后一个工作日生成；要补生成请加 --force。")
        return 0

    period_key = _summary_period_key(kind, run_date)
    if not args.force and database.get_summary_report(kind, period_key):
        print(f"{label}已生成过（{period_key}）；要重生成请加 --force。")
        return 0

    result = _generate_summary_if_due(
        kind,
        database,
        config,
        client,
        run_date_text,
        force=args.force,
    )
    if not result:
        print(f"{label}没有生成：还没有可用快照，先运行 run。")
        return 1
    path, summary, _ = result
    if not args.no_notify:
        full_report_mode = _full_report_mode(config)
        if full_report_mode in {"review", "all"}:
            content = path.read_text(encoding="utf-8")
            notify_report(config, f"基金{label}", summary, content)
        else:
            notify(config, f"基金{label}", summary)
    print(summary)
    print(f"{label}：{path}")
    return 0


def cmd_weekly(args: argparse.Namespace) -> int:
    return _cmd_summary("weekly", args)


def cmd_monthly(args: argparse.Namespace) -> int:
    return _cmd_summary("monthly", args)


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    _run_started = time.monotonic()
    _ledger_path = BASE_DIR / config.get("data_dir", "data") / "run_ledger.jsonl"
    RunLedger(_ledger_path).record(job="fund-run", event="start")
    if is_holiday_weekday(run_date):
        message = f"{run_date_text} 是工作日但 A 股休市（法定节假日），按规则不生成新报告。"
        print(message)
        RunLedger(_ledger_path).record(
            job="fund-run",
            event="done",
            status="skipped",
            notes="weekday holiday - A 股休市日不生成报告",
        )
        return 0
    funds = database.list_funds()
    if not funds:
        print("跟踪列表为空。先运行：python3 -m fund_tracker add 000032")
        return 1

    client = _client(config)
    if args.force:
        client.default_cache_hours = 0

    rows: list[dict] = []
    all_alerts: list[dict] = []
    errors: list[str] = []

    for index, fund in enumerate(funds, 1):
        code = fund["code"]
        print(f"[{index}/{len(funds)}] {code} {fund['name']} ...")
        try:
            fund_data = fetch_fund(client, code)
            try:
                profile = fetch_profile(client, code)
            except FetchError:
                profile = {}
            metrics = build_metrics(fund_data, profile, config, today=run_date)
            if fund.get("category"):
                metrics["category"] = normalize_category(fund["category"]) or fund["category"]
            if fund.get("role"):
                metrics["role"] = fund["role"]
            if fund.get("target_weight") is not None:
                metrics["target_weight"] = fund["target_weight"]

            net_series = parse_net_worth(fund_data.get("net_worth"))
            ac_series = parse_ac_worth(fund_data.get("ac_worth"))
            database.save_nav(code, net_series, ac_series)
            database.save_source_health("fund_nav", run_date_text, code, True)

            extras_cache = float(config.get("extras_cache_hours", 24))
            try:
                holdings = fetch_fund_holdings(
                    client,
                    code,
                    cache_hours=extras_cache,
                )
                stock_codes = [
                    str(item.get("code"))
                    for item in holdings.get("stock") or []
                    if item.get("code")
                ][:10]
                quotes = fetch_stock_quotes(client, stock_codes) if stock_codes else {}
                for item in holdings.get("stock") or []:
                    quote = quotes.get(str(item.get("code")))
                    if quote and quote.get("pct") is not None:
                        item["pct"] = quote["pct"]
                metrics["holdings"] = holdings
                database.save_source_health("fund_holdings", run_date_text, code, True)
            except Exception as exc:  # noqa: BLE001
                metrics["holdings_error"] = str(exc)
                database.save_source_health("fund_holdings", run_date_text, code, False, str(exc)[:200])

            try:
                announcements = fetch_fund_announcements(
                    client,
                    code,
                    limit=100,
                    cache_hours=extras_cache,
                )
                metrics["announcements"] = _select_announcements(announcements)
                database.save_source_health("fund_announcements", run_date_text, code, True)
            except Exception as exc:  # noqa: BLE001
                metrics["announcements_error"] = str(exc)
                database.save_source_health("fund_announcements", run_date_text, code, False, str(exc)[:200])

            previous = database.get_previous_snapshot(code, run_date_text)
            fund_alerts = _build_alerts(metrics, previous, config)
            fund_alerts.extend(_announcement_alerts(metrics))
            metrics["alert_count"] = len(fund_alerts)
            database.save_snapshot(code, run_date_text, metrics)

            rows.append(metrics)
            all_alerts.extend(fund_alerts)
            daily_text = (
                f" 日涨跌 {metrics['daily_pct']:+.2f}%"
                if metrics.get("daily_pct") is not None
                else ""
            )
            print(
                f"  净值 {metrics['nav']:.4f}（{metrics['nav_date']}）"
                f"{daily_text} 评分 {metrics['score']:.1f}"
            )
        except Exception as exc:  # noqa: BLE001 - one fund should not break the run
            message = f"{code} {fund['name']}: {exc}"
            errors.append(message)
            print(f"  失败：{exc}")
        time.sleep(0.2)

    market_success = False
    try:
        market = fetch_market(client)
        market_success = True
        database.save_source_health("market_index", run_date_text, None, True)
    except FetchError as exc:
        market = []
        database.save_source_health("market_index", run_date_text, None, False, str(exc)[:200])
    macro_snapshot: dict = {}
    macro_section = ""
    if config.get("macro", {}).get("enabled", True):
        try:
            macro_snapshot = fetch_macro_snapshot(
                client,
                news_limit=int(config.get("macro", {}).get("news_limit", 8)),
                include_news=bool(
                    config.get("macro", {}).get("news_enabled", True)
                ),
            )
            macro_section = build_macro_section(macro_snapshot, config)
            database.save_source_health("macro", run_date_text, None, True)
        except Exception as exc:  # noqa: BLE001
            print(f"宏观数据获取失败，日报继续：{exc}")
            database.save_source_health("macro", run_date_text, None, False, str(exc)[:200])
    alerts_by_code: dict[str, list[dict]] = {}
    for alert in all_alerts:
        alerts_by_code.setdefault(alert.get("code", ""), []).append(alert)
    evidence = build_evidence(rows, macro_snapshot, alerts_by_code)
    # B1: compute data_date from actual NAV dates, not run_date
    nav_dates = [str(row["nav_date"]) for row in rows if row.get("nav_date")]
    data_date = max(nav_dates) if nav_dates else run_date_text
    ai_section = _ai_daily_section(
        database,
        config,
        macro_snapshot,
        run_date_text,
    )
    learning_section = _learning_context(database, run_date_text)
    rows.sort(key=lambda item: (item.get("category") or "其他", -(item.get("score") or 0)))
    holdings, snapshot = _holdings_with_snapshot(config, rows)
    data_quality = assess_freshness(rows, holdings, config)
    report = generate_report(
        run_date_text,
        rows,
        market,
        all_alerts,
        config,
        macro_section=macro_section,
        database=database,
        evidence=evidence,
        learning_section=learning_section,
        ai_section=ai_section,
        holdings=holdings,
        snapshot=snapshot,
        data_quality=data_quality,
    )
    report_path = save_report(
        BASE_DIR / config["report_dir"],
        run_date_text,
        report,
    )
    database.save_alerts(run_date_text, all_alerts)
    database.export_csv(
        BASE_DIR / config["report_dir"] / f"{run_date_text}.csv",
        run_date_text,
    )
    enriched_rows = _enrich_recommendations(rows, alerts_by_code, evidence)
    summary = summary_text(
        run_date_text,
        enriched_rows,
        all_alerts,
        str(report_path),
        holdings=holdings,
    )
    status = "ok" if not errors else ("partial" if rows else "failed")
    if data_quality.get("level") == "stale":
        status = "stale"
    database.save_run(run_date_text, status, summary)

    full_report_mode = _full_report_mode(config)
    notify_cfg = config.get("notify") or {}
    webhook_configured = bool((notify_cfg.get("webhook_url") or "").strip())
    sent = False
    stale = data_quality.get("level") == "stale"
    if stale:
        summary = "【数据不完整·本期不推送正常结论】" + summary
        print(freshness_banner(data_quality))
    if not args.no_notify and not stale:
        if full_report_mode == "all":
            sent = notify_report(config, "基金跟踪日报", summary, report)
        else:
            sent = notify(config, "基金跟踪日报", summary)

    RunLedger(_ledger_path).record(
        job="fund-run",
        event="done",
        status=status,
        report_path=str(report_path),
        notify={"sent": bool(sent), "webhook": webhook_configured},
        counts={"rows": len(rows), "alerts": len(all_alerts), "errors": len(errors)},
        errors=errors,
        data_date=max(nav_dates, default=None),
        duration_ms=int((time.monotonic() - _run_started) * 1000),
    )

    # B1: only generate review when data_date matches run_date
    review_data_date = data_date if data_date == run_date_text else None
    if rows and review_data_date and config.get("review", {}).get("auto", True):
        try:
            review_result = _generate_review_if_due(
                database,
                config,
                client,
                review_data_date,
                macro_snapshot=macro_snapshot,
                macro_section=macro_section,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{_review_label(config)}生成失败：{exc}")
            review_result = None
        if review_result:
            review_path, review_summary, _ = review_result
            label = _review_label(config)
            print(review_summary)
            print(f"{label}：{review_path}")
            if not args.no_notify:
                if full_report_mode in {"review", "all"}:
                    review_content = review_path.read_text(encoding="utf-8")
                    notify_report(
                        config,
                        f"基金{label}",
                        review_summary,
                        review_content,
                    )
                else:
                    notify(config, f"基金{label}", review_summary)

    if rows and config.get("summary", {}).get("enabled", True):
        for kind, label in (("weekly", "周度总结"), ("monthly", "月度总结")):
            try:
                summary_result = _generate_summary_if_due(
                    kind,
                    database,
                    config,
                    client,
                    run_date_text,
                    macro_snapshot=macro_snapshot,
                    macro_section=macro_section,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{label}生成失败：{exc}")
                summary_result = None
            if not summary_result:
                continue
            summary_path, summary_message, _ = summary_result
            print(summary_message)
            print(f"{label}：{summary_path}")
            if not args.no_notify:
                if full_report_mode in {"review", "all"}:
                    summary_content = summary_path.read_text(encoding="utf-8")
                    notify_report(
                        config,
                        f"基金{label}",
                        summary_message,
                        summary_content,
                    )
                else:
                    notify(config, f"基金{label}", summary_message)

    print()
    print(summary)
    (Path(config["report_dir"]) / "latest.md").write_text(report, encoding="utf-8")

    print(f"报告：{report_path}")
    print(f"CSV：{BASE_DIR / config['report_dir'] / (run_date_text + '.csv')}")
    if errors:
        print(f"\n有 {len(errors)} 只基金失败：")
        for error in errors:
            print(f"  - {error}")
    return 0 if rows else 1


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    rows = database.get_snapshots(run_date_text)
    if not rows:
        print(f"{run_date_text} 没有快照。先运行 run。")
        return 1
    holdings = attach_holdings(rows, load_holdings(config))
    alerts = database.get_alerts(run_date_text)
    client = _client(config)
    market_success = False
    try:
        market = fetch_market(client)
        market_success = True
        database.save_source_health("market_index", run_date_text, None, True)
    except FetchError as exc:
        market = []
        database.save_source_health("market_index", run_date_text, None, False, str(exc)[:200])
    macro_snapshot: dict = {}
    macro_section = ""
    if config.get("macro", {}).get("enabled", True):
        try:
            macro_snapshot = fetch_macro_snapshot(
                client,
                news_limit=int(config.get("macro", {}).get("news_limit", 8)),
                include_news=bool(
                    config.get("macro", {}).get("news_enabled", True)
                ),
            )
            macro_section = build_macro_section(macro_snapshot, config)
        except Exception as exc:  # noqa: BLE001
            print(f"宏观数据获取失败，报告继续：{exc}")
    alerts_by_code: dict[str, list[dict]] = {}
    for alert in alerts:
        alerts_by_code.setdefault(alert.get("code", ""), []).append(alert)
    evidence = build_evidence(rows, macro_snapshot, alerts_by_code)
    # B1: compute data_date from actual NAV dates, not run_date
    nav_dates = [str(row["nav_date"]) for row in rows if row.get("nav_date")]
    data_date = max(nav_dates) if nav_dates else run_date_text
    ai_section = _ai_daily_section(
        database,
        config,
        macro_snapshot,
        run_date_text,
    )
    learning_section = _learning_context(database, run_date_text)
    report = generate_report(
        run_date_text,
        rows,
        market,
        alerts,
        config,
        macro_section=macro_section,
        evidence=evidence,
        learning_section=learning_section,
        ai_section=ai_section,
        holdings=holdings,
    )
    path = save_report(BASE_DIR / config["report_dir"], run_date_text, report)
    print(f"已生成：{path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    client = _client(config)
    ok = True
    checks = []

    try:
        fund = fetch_fund(client, "000032")
        checks.append(("基金净值接口", f"OK - {fund['name']}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("基金净值接口", f"FAIL - {exc}"))
        ok = False

    try:
        ranking = fetch_ranking(client, "zq", pages=1, page_size=3)
        checks.append(("基金排行榜接口", f"OK - {len(ranking)} 条"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("基金排行榜接口", f"FAIL - {exc}"))
        ok = False

    try:
        market = fetch_market(client)
        checks.append(("指数行情接口", f"OK - {len(market)} 个指数"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("指数行情接口", f"FAIL - {exc}"))
        ok = False

    try:
        macro = fetch_macro_snapshot(client, news_limit=3)
        cn10 = (macro.get("china") or {}).get("10y")
        us10 = (macro.get("global") or {}).get("us_10y")
        checks.append(
            (
                "宏观/新闻接口",
                f"OK - 中国10Y {cn10 if cn10 is not None else '—'} / "
                f"美国10Y {us10 if us10 is not None else '—'} / "
                f"新闻 {len(macro.get('news') or [])} 条",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(("宏观/新闻接口", f"FAIL - {exc}"))
        ok = False

    for name, result in checks:
        print(f"{name}: {result}")
    print("\n全部正常。" if ok else "\n有接口异常，请检查网络或稍后重试。")
    return 0 if ok else 1


def cmd_test_notify(args: argparse.Namespace) -> int:
    config = load_config()
    notify_config = config.get("notify", {})
    title = "基金跟踪测试"
    message = "如果你看到这条消息，说明通知配置成功。"

    if args.full:
        report_path = BASE_DIR / config["report_dir"] / "review-latest.md"
        if not report_path.exists():
            print(f"找不到完整报告：{report_path}")
            return 1
        webhook_url = (notify_config.get("webhook_url") or "").strip()
        if not webhook_url:
            print("config.json 的 notify.webhook_url 为空。")
            return 1
        report_text = report_path.read_text(encoding="utf-8")
        test_config = dict(config)
        test_notify_config = dict(notify_config)
        if args.webhook_only:
            test_notify_config["macos"] = False
        test_config["notify"] = test_notify_config
        ok = notify_report(
            test_config,
            f"基金{_review_label(config)}（完整版测试）",
            "完整报告推送测试",
            report_text,
        )
        print(f"完整版推送: {'成功' if ok else '失败'}")
        return 0 if ok else 1

    results: list[tuple[str, bool]] = []

    if not args.webhook_only and notify_config.get("macos"):
        results.append(("macOS 通知", macos_notify(title, message)))

    webhook_url = (notify_config.get("webhook_url") or "").strip()
    if webhook_url:
        secret = (
            notify_config.get("webhook_secret")
            or notify_config.get("feishu_secret")
            or ""
        ).strip()
        label = "飞书机器人" if is_feishu_webhook(webhook_url) else "Webhook"
        results.append(
            (
                label,
                webhook_notify(webhook_url, title, message, secret=secret),
            )
        )
    elif args.webhook_only:
        print("config.json 的 notify.webhook_url 为空。")
        return 1

    if not results:
        print("没有启用通知：设置 notify.macos=true 或填入 notify.webhook_url。")
        return 1

    for name, ok in results:
        print(f"{name}: {'成功' if ok else '失败'}")
    return 0 if any(ok for _, ok in results) else 1


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _ensure_launchd_dir() -> Path:
    LAUNCHD_DIR.parent.mkdir(parents=True, exist_ok=True)
    if LAUNCHD_DIR.is_symlink():
        LAUNCHD_DIR.unlink()
    elif LAUNCHD_DIR.exists() and not LAUNCHD_DIR.is_dir():
        raise RuntimeError(
            f"{LAUNCHD_DIR} 已存在且不是目录；请先移走它。"
        )
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = LAUNCHD_DIR / "run_daily.sh"
    python_bin = Path(sys.executable).resolve()
    wrapper.write_text(
        "#!/bin/zsh\n"
        f'cd "{BASE_DIR}" || exit 1\n'
        f'PYTHON_BIN="{python_bin}"\n'
        'if [ ! -x "$PYTHON_BIN" ]; then\n'
        '  PYTHON_BIN="$(command -v python3)"\n'
        "fi\n"
        "exec >> data/cron.log 2>&1\n"
        'exec "$PYTHON_BIN" -m fund_tracker run "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return LAUNCHD_DIR


def _plist_content(config: dict, launchd_dir: Path | None = None) -> str:
    hour = int(config.get("schedule", {}).get("hour", 22))
    minute = int(config.get("schedule", {}).get("minute", 30))
    launchd_dir = launchd_dir or LAUNCHD_DIR
    script = launchd_dir / "run_daily.sh"
    log_dir = launchd_dir / config["data_dir"]
    weekdays = "".join(
        f"""
        <dict>
            <key>Weekday</key><integer>{day}</integer>
            <key>Hour</key><integer>{hour}</integer>
            <key>Minute</key><integer>{minute}</integer>
        </dict>"""
        for day in range(1, 6)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>{script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{launchd_dir}</string>
    <key>StartCalendarInterval</key>
    <array>{weekdays}
    </array>
    <key>StandardOutPath</key>
    <string>{log_dir / "launchd.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "launchd.err.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
"""


def cmd_schedule(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    plist_path = _plist_path()
    uid = os.getuid()
    domain = f"gui/{uid}"

    if args.status:
        result = subprocess.run(
            ["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        print(result.stdout or result.stderr or "未安装定时任务。")
        return 0 if result.returncode == 0 else 1

    if args.uninstall:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if plist_path.exists():
            plist_path.unlink()
        print("已卸载定时任务。")
        return 0

    if args.install:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            launchd_dir = _ensure_launchd_dir()
        except RuntimeError as exc:
            print(str(exc))
            return 1
        script = launchd_dir / "run_daily.sh"
        if not script.exists():
            print(f"找不到启动脚本：{script}")
            return 1
        script.chmod(0o755)
        plist_path.write_text(
            _plist_content(config, launchd_dir),
            encoding="utf-8",
        )
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"安装失败：{result.stderr.strip()}")
            return 1
        hour = config.get("schedule", {}).get("hour", 22)
        minute = config.get("schedule", {}).get("minute", 30)
        print(f"已安装：每个工作日 {hour:02d}:{minute:02d} 自动运行")
        print(f"plist：{plist_path}")
        print("查看状态：python3 -m fund_tracker schedule --status")
        return 0

    print("请指定 --install / --uninstall / --status")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fund-tracker",
        description="本地基金跟踪工具：抓公开净值、算风险指标、生成每日报告。",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化目录和数据库")
    init_parser.set_defaults(func=cmd_init)

    add_parser = subparsers.add_parser("add", help="添加基金（代码或名称关键词）")
    add_parser.add_argument("code", help="6 位基金代码，或名称关键词")
    add_parser.add_argument("--category", help="手动指定类别：短债/纯债/二级债/红利/宽基")
    add_parser.add_argument("--role", help="角色：核心/卫星/观察")
    add_parser.add_argument("--weight", type=float, help="目标权重，例如 0.2")
    add_parser.add_argument("--note", help="备注")
    add_parser.set_defaults(func=cmd_add)

    remove_parser = subparsers.add_parser("remove", help="移除基金")
    remove_parser.add_argument("code")
    remove_parser.set_defaults(func=cmd_remove)

    list_parser = subparsers.add_parser("list", help="查看跟踪列表")
    list_parser.set_defaults(func=cmd_list)

    discover_parser = subparsers.add_parser("discover", help="从排行榜筛选候选基金")
    discover_parser.add_argument("--type", default="zq", choices=["zq", "hh", "zs", "gp"], help="zq=债券 hh=混合 zs=指数 gp=股票")
    discover_parser.add_argument("--subtype", help="债券子类：041 长期纯债，042 短期纯债，043 混合债基，045 可转债")
    discover_parser.add_argument("--keyword", action="append", help="名称关键词，可重复，匹配任意一个")
    discover_parser.add_argument("--pages", type=int, default=3, help="抓取排行榜页数")
    discover_parser.add_argument("--page-size", type=int, default=50)
    discover_parser.add_argument("--limit", type=int, default=20, help="展示数量")
    discover_parser.add_argument("--min-size", type=float, default=2.0, help="最小规模（亿）")
    discover_parser.add_argument("--max-size", type=float, default=200.0, help="最大规模（亿）")
    discover_parser.add_argument("--min-age", type=float, default=3.0, help="最小成立年限")
    discover_parser.add_argument("--include-convertible", action="store_true", help="包含可转债基金")
    discover_parser.add_argument("--details", action="store_true", help="抓详情并计算评分（较慢）")
    discover_parser.add_argument("--add-top", type=int, default=0, help="把评分前 N 只加入跟踪")
    discover_parser.add_argument("--category", help="加入跟踪时指定类别")
    discover_parser.add_argument("--role", default="观察", help="加入跟踪时的角色")
    discover_parser.set_defaults(func=cmd_discover)

    run_parser = subparsers.add_parser(
        "run",
        help="抓取数据并生成日报；到期自动生成 2 日/周/月推荐",
    )
    run_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    run_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    run_parser.add_argument("--force", action="store_true", help="忽略缓存，强制重新抓取")
    run_parser.set_defaults(func=cmd_run)

    report_parser = subparsers.add_parser("report", help="用已有快照重新生成报告")
    report_parser.add_argument("--date", help="指定日期 YYYY-MM-DD")
    report_parser.set_defaults(func=cmd_report)

    review_parser = subparsers.add_parser(
        "review",
        help="生成推荐与买卖参考区间（默认每 2 个交易日）",
    )
    review_parser.add_argument("--date", help="指定日期 YYYY-MM-DD")
    review_parser.add_argument("--force", action="store_true", help="不到期也强制生成")
    review_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    review_parser.set_defaults(func=cmd_review)

    weekly_parser = subparsers.add_parser(
        "weekly",
        help="生成周五周度总结推荐",
    )
    weekly_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    weekly_parser.add_argument("--force", action="store_true", help="不是周五也强制生成")
    weekly_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    weekly_parser.set_defaults(func=cmd_weekly)

    monthly_parser = subparsers.add_parser(
        "monthly",
        help="生成每月最后一个工作日的月度总结推荐",
    )
    monthly_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    monthly_parser.add_argument("--force", action="store_true", help="不是月末工作日也强制生成")
    monthly_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    monthly_parser.set_defaults(func=cmd_monthly)

    check_parser = subparsers.add_parser("check", help="检查数据接口")
    check_parser.set_defaults(func=cmd_check)

    test_notify_parser = subparsers.add_parser("test-notify", help="发送一条测试通知")
    test_notify_parser.add_argument(
        "--webhook-only",
        action="store_true",
        help="只测试 Webhook/飞书，不弹 macOS 通知",
    )
    test_notify_parser.add_argument(
        "--full",
        action="store_true",
        help="发送 reports/review-latest.md 的完整版测试",
    )
    test_notify_parser.set_defaults(func=cmd_test_notify)

    schedule_parser = subparsers.add_parser("schedule", help="安装/卸载 macOS 定时任务")
    schedule_group = schedule_parser.add_mutually_exclusive_group()
    schedule_group.add_argument("--install", action="store_true")
    schedule_group.add_argument("--uninstall", action="store_true")
    schedule_group.add_argument("--status", action="store_true")
    schedule_parser.set_defaults(func=cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"运行失败：{exc}", file=sys.stderr)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
