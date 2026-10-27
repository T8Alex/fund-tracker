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
from fund_tracker.ai_digest import (
    build_daily_digest,
    build_weekly_digest,
    render_ai_error,
    render_ai_section,
    render_weekly_ai_section,
)
from fund_tracker.config import BASE_DIR, load_config as load_fund_config
from fund_tracker.fetch import (
    HttpClient,
    fetch_market,
    fetch_market_history,
    fetch_market_history_range,
)
from fund_tracker.learning import (
    evaluate_predictions,
    record_predictions,
    render_learning_section,
    summarize_predictions,
)
from fund_tracker.macro import fetch_macro_snapshot
from fund_tracker.notify import is_feishu_webhook, notify, notify_report

from .config import STOCK_CONFIG_PATH, ensure_dirs, load_config, save_config
from .evidence import build_evidence
from .fetch import fetch_stock, is_stock_code, search_stock
from .holdings import attach_holdings, holdings_path, load_holdings
from .metrics import build_metrics
from .report import (
    build_macro_section,
    enrich_rows,
    generate_daily_report,
    generate_review,
    generate_summary_report,
    next_month_last_weekday,
    next_weekday_date,
    summary_text_daily,
    summary_text_review,
    summary_text_summary,
)
from .store import Database

LOG = logging.getLogger(__name__)
LAUNCHD_LABEL = "com.t8alex.stocktracker"
LAUNCHD_DIR = Path.home() / ".local" / "share" / "stock-tracker"


def _db(config: dict) -> Database:
    database = Database(BASE_DIR / config["data_dir"] / "stock_tracker.db")
    database.init()
    return database


def _client(config: dict) -> HttpClient:
    return HttpClient(config)


def _learning_series_loader(database: Database):
    def load(code: str, start_date: str, end_date: str):
        return [
            {"date": row.get("date"), "close": row.get("close")}
            for row in database.get_price_range(code, start_date, end_date)
        ]

    return load


def _learning_context(
    database: Database,
    as_of: str,
    *,
    module: str = "股票",
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
            price_key="price",
            module_label="股票",
        )
    except Exception as exc:  # noqa: BLE001 - a log failure is not a report failure
        print(f"学习记录失败：{exc}")


def _ai_daily_section(
    database: Database,
    config: dict,
    macro_snapshot: dict | None,
    run_date_text: str,
) -> str:
    """B 阶段：只读取本项目新闻/摘要，交给本地模型做事实提炼。"""
    ai_config = config.get("ai") or {}
    if not ai_config.get("enabled", False):
        return ""
    try:
        result = build_daily_digest(
            db_path=database.path,
            module="股票",
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
            module="股票",
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


def _fund_webhook_url() -> str:
    """Return the fund robot URL, if configured, for an isolation guard."""
    try:
        config = load_fund_config()
    except Exception:  # noqa: BLE001
        return ""
    return str((config.get("notify") or {}).get("webhook_url") or "").strip()


def _webhook_reuses_fund(config: dict) -> bool:
    stock_url = str((config.get("notify") or {}).get("webhook_url") or "").strip()
    fund_url = _fund_webhook_url()
    return bool(stock_url and fund_url and stock_url == fund_url)


def _drop_reused_webhook(config: dict) -> bool:
    if not _webhook_reuses_fund(config):
        return False
    config.setdefault("notify", {})["webhook_url"] = ""
    return True

def _weight(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number > 1:
        number /= 100.0
    if number < 0 or number > 1:
        raise ValueError("目标权重必须在 0–1（或 0–100%）之间")
    return round(number, 4)


def _weight_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


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
    interval = int(config.get("review", {}).get("interval_trading_days", 2))
    last_review = database.get_last_review()
    if last_review:
        period_start = last_review.get("review_date") or last_review.get("period_end")
        days = database.count_trading_days_since(period_start, run_date_text)
        return days >= interval, period_start, days, interval, last_review
    period_start = (
        date.fromisoformat(run_date_text) - timedelta(days=7)
    ).isoformat()
    return True, period_start, interval, interval, None

def _save_daily(report_dir: Path, run_date: str, content: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"
    path.write_text(content, encoding="utf-8")
    (report_dir / "latest.md").write_text(content, encoding="utf-8")
    return path


def _save_review(report_dir: Path, review_date: str, content: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"review-{review_date}.md"
    path.write_text(content, encoding="utf-8")
    (report_dir / "review-latest.md").write_text(content, encoding="utf-8")
    return path


def _save_summary(report_dir: Path, kind: str, report_date: str, content: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"monthly-{report_date[:7]}.md"
        if kind == "monthly"
        else f"weekly-{report_date}.md"
    )
    path = report_dir / filename
    path.write_text(content, encoding="utf-8")
    (report_dir / f"{kind}-latest.md").write_text(content, encoding="utf-8")
    return path


def _macro_context(client: HttpClient, config: dict) -> tuple[dict, str]:
    if not config.get("macro", {}).get("enabled", True):
        return {}, ""
    try:
        snapshot = fetch_macro_snapshot(
            client,
            news_limit=int(config.get("macro", {}).get("news_limit", 8)),
            include_news=bool(config.get("macro", {}).get("news_enabled", True)),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"宏观数据获取失败，报告继续：{exc}")
        return {}, ""
    return snapshot, build_macro_section(snapshot)


def _build_alerts(metrics: dict, previous: dict | None, config: dict) -> list[dict]:
    alerts: list[dict] = []
    code = metrics.get("code")
    name = metrics.get("name")
    alert_config = config.get("alerts", {})

    def add(level: str, message: str) -> None:
        alerts.append(
            {"code": code, "name": name, "level": level, "message": message}
        )

    daily_pct = metrics.get("daily_pct")
    daily_limit = float(alert_config.get("daily_move_pct", 5.0))
    if daily_pct is not None and abs(daily_pct) >= daily_limit:
        add(
            "warn",
            f"单日{'上涨' if daily_pct > 0 else '下跌'} {abs(daily_pct):.2f}%，"
            f"超过 {daily_limit:.1f}% 阈值",
        )

    dd_1y = metrics.get("dd_1y")
    dd_1y_limit = float(alert_config.get("dd_1y_pct", 20.0))
    if dd_1y is not None and abs(dd_1y) >= dd_1y_limit:
        add("high", f"近1年最大回撤 {dd_1y:.2f}%，超过 {dd_1y_limit:.1f}%")
    elif dd_1y is not None and abs(dd_1y) >= dd_1y_limit * 0.75:
        add("warn", f"近1年最大回撤 {dd_1y:.2f}%，接近阈值")

    dd_3y = metrics.get("dd_3y")
    dd_3y_limit = float(alert_config.get("dd_3y_pct", 35.0))
    if dd_3y is not None and abs(dd_3y) >= dd_3y_limit:
        add("high", f"近3年最大回撤 {dd_3y:.2f}%，超过 {dd_3y_limit:.1f}%")

    score = metrics.get("score")
    prev_score = (previous or {}).get("score")
    score_drop = float(alert_config.get("score_drop", 8.0))
    if score is not None and prev_score is not None and prev_score - score >= score_drop:
        add("warn", f"评分较上次下降 {prev_score - score:.1f} 分（{prev_score:.1f}→{score:.1f}）")

    spike = metrics.get("volume_spike")
    spike_limit = float(alert_config.get("volume_spike", 2.0))
    if spike is not None and spike >= spike_limit:
        add("warn", f"成交量约为20日均量的 {spike:.1f} 倍，关注异动")

    cap = metrics.get("market_cap_yi")
    cap_min = float(alert_config.get("market_cap_min_yi", 50.0))
    if cap is not None and cap < cap_min:
        add("warn", f"市值约 {cap:.1f} 亿，低于 {cap_min:.0f} 亿小盘线")

    pe = metrics.get("pe")
    pe_max = float(alert_config.get("pe_max", 80.0))
    if pe is not None and pe < 0:
        add("warn", "PE 为负，当前处于亏损状态")
    elif pe is not None and pe > pe_max:
        add("warn", f"PE {pe:.1f}，高于 {pe_max:.0f} 倍估值线")

    if "ST" in str(name).upper() or "退" in str(name):
        add("high", "名称含 ST/退市风险标记")

    stale = metrics.get("stale_days")
    if stale is not None and stale > int(config.get("stale_days", 5)):
        add("warn", f"行情已 {stale} 天未更新，注意停牌/数据延迟")

    tech = metrics.get("tech") or {}
    ma60 = tech.get("ma60")
    price = metrics.get("price")
    if ma60 and price is not None and price < float(ma60) * 0.90:
        add("warn", f"价格低于60日均线 {abs(price / float(ma60) - 1) * 100:.1f}%")

    from_high = metrics.get("from_high_1y")
    if from_high is not None and from_high <= -30:
        add("info", f"距近1年高点 {from_high:.2f}%")

    return alerts


HIGH_ANNOUNCEMENT_WORDS = (
    "立案",
    "处罚",
    "退市",
    "减持",
    "质押",
    "预亏",
    "亏损",
    "终止",
    "风险提示",
    "诉讼",
    "冻结",
    "违规",
    "问询",
    "监管",
)
WARN_ANNOUNCEMENT_WORDS = (
    "业绩预告",
    "业绩快报",
    "年报",
    "半年报",
    "季报",
    "分红",
    "回购",
    "增持",
    "定增",
    "收购",
    "重组",
    "高管",
    "辞职",
    "重大合同",
)


def _announcement_alerts(
    metrics: dict,
    config: dict,
    as_of: date | None = None,
) -> list[dict]:
    alerts: list[dict] = []
    code = metrics.get("code")
    name = metrics.get("name")
    days_limit = int(config.get("alerts", {}).get("announcement_days", 45))
    today = as_of or date.today()
    seen: set[str] = set()
    for item in metrics.get("announcements") or []:
        title = str(item.get("title") or "").strip()
        if not title or title in seen:
            continue
        date_text = str(item.get("date") or "")[:10]
        try:
            published = date.fromisoformat(date_text)
        except ValueError:
            continue
        if (today - published).days > days_limit:
            continue
        seen.add(title)
        if any(word in title for word in HIGH_ANNOUNCEMENT_WORDS):
            level = "high"
        elif any(word in title for word in WARN_ANNOUNCEMENT_WORDS):
            level = "warn"
        else:
            continue
        alerts.append(
            {
                "code": code,
                "name": name,
                "level": level,
                "message": f"公告：{title}（{date_text}）",
            }
        )
        if len(alerts) >= 3:
            break
    return alerts


def _attach_meta(row: dict, stock: dict) -> None:
    for key in ("role", "target_weight", "category", "note"):
        value = stock.get(key)
        if value is not None and value != "":
            row[key] = value
    if stock.get("name"):
        row["name"] = stock["name"]
    if not row.get("industry") and stock.get("category"):
        row["industry"] = stock["category"]


def _build_review_rows(
    database: Database,
    config: dict,
    run_date_text: str,
    period_start: str,
    window_days: int = 2,
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for stock in database.list_stocks():
        code = stock["code"]
        current = database.get_snapshot_on_or_before(code, run_date_text)
        if not current:
            continue
        row = dict(current)
        _attach_meta(row, stock)
        window = database.get_price_window(
            code, run_date_text, max(2, window_days + 1)
        )
        if len(window) >= 2:
            start = window[0].get("close")
            end = window[-1].get("close")
            if start and end and float(start) > 0:
                ret = round((float(end) / float(start) - 1) * 100, 2)
                row["ret_window"] = ret
                row["ret_5d"] = ret
                row["period_days"] = len(window) - 1
                row["period_label"] = f"{window_days} 日"
                row["price_start_date"] = window[0].get("date")
        previous = None
        if period_start < run_date_text:
            previous = database.get_snapshot_on_or_before(code, period_start)
        if (
            previous
            and previous.get("score") is not None
            and row.get("score") is not None
        ):
            delta = round(float(row["score"]) - float(previous["score"]), 1)
            row["score_delta_window"] = delta
            row["score_delta_5d"] = delta
        rows.append(row)
    holdings = attach_holdings(rows, load_holdings(config))
    return rows, holdings


def _build_period_rows(
    database: Database,
    config: dict,
    report_date_text: str,
    period_start: str,
    return_start: str,
    period_label: str,
) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for stock in database.list_stocks():
        code = stock["code"]
        current = database.get_snapshot_on_or_before(code, report_date_text)
        if not current:
            continue
        row = dict(current)
        _attach_meta(row, stock)
        prices = database.get_price_range(code, return_start, report_date_text)
        if len(prices) >= 2:
            start = prices[0].get("close")
            end = prices[-1].get("close")
            if start and end and float(start) > 0:
                ret = round((float(end) / float(start) - 1) * 100, 2)
                row["ret_window"] = ret
                row["ret_5d"] = ret
                row["period_days"] = len(prices) - 1
                row["period_label"] = period_label
                row["price_start_date"] = prices[0].get("date")
        previous = None
        if period_start < report_date_text:
            previous = database.get_snapshot_on_or_before(code, period_start)
        if (
            previous
            and previous.get("score") is not None
            and row.get("score") is not None
        ):
            delta = round(float(row["score"]) - float(previous["score"]), 1)
            row["score_delta_window"] = delta
            row["score_delta_5d"] = delta
        rows.append(row)
    holdings = attach_holdings(rows, load_holdings(config))
    return rows, holdings


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
        database, run_date_text, config
    )
    if not due and not force:
        return None
    rows, holdings = _build_review_rows(
        database, config, run_date_text, period_start, window_days=interval
    )
    if not rows:
        return None
    start_dates = [
        row.get("price_start_date")
        for row in rows
        if row.get("price_start_date")
    ]
    report_start = (
        Counter(start_dates).most_common(1)[0][0] if start_dates else period_start
    )
    alerts = database.get_alerts_on_or_before(run_date_text)
    market = fetch_market_history(client, count=interval + 1)
    if macro_snapshot is None:
        macro_snapshot, macro_section = _macro_context(client, config)
    elif macro_section is None:
        macro_section = build_macro_section(macro_snapshot) if macro_snapshot else ""
    evidence = build_evidence(rows, macro_snapshot)
    enriched = enrich_rows(rows, alerts, evidence)
    _record_learning(database, run_date_text, enriched)
    ai_section = _ai_daily_section(
        database,
        config,
        macro_snapshot,
        run_date_text,
    )
    learning_section = _learning_context(database, run_date_text)
    next_date = next_weekday_date(run_date_text, interval)
    content = generate_review(
        run_date_text,
        report_start,
        run_date_text,
        rows,
        market,
        alerts,
        config,
        next_review_date=next_date,
        macro_section=macro_section or "",
        evidence=evidence,
        window_days=interval,
        holdings=holdings,
        learning_section=learning_section,
        ai_section=ai_section,
    )
    path = _save_review(BASE_DIR / config["report_dir"], run_date_text, content)
    summary = summary_text_review(run_date_text, enriched, alerts, str(path))
    database.save_review(run_date_text, report_start, run_date_text, summary, str(path))
    return path, summary, enriched


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

    rows, holdings = _build_period_rows(
        database,
        config,
        report_date_text,
        period_start,
        return_start,
        period_label,
    )
    if not rows:
        return None
    start_dates = [
        row.get("price_start_date")
        for row in rows
        if row.get("price_start_date")
    ]
    report_start = (
        Counter(start_dates).most_common(1)[0][0] if start_dates else return_start
    )
    alerts = database.get_alerts_on_or_before(report_date_text)
    market = fetch_market_history_range(client, return_start, report_date_text)
    if macro_snapshot is None:
        macro_snapshot, macro_section = _macro_context(client, config)
    elif macro_section is None:
        macro_section = build_macro_section(macro_snapshot) if macro_snapshot else ""
    evidence = build_evidence(rows, macro_snapshot)
    enriched = enrich_rows(rows, alerts, evidence)
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
        macro_section=macro_section or "",
        evidence=evidence,
        holdings=holdings,
        learning_section=learning_section,
        ai_section=ai_section,
    )
    path = _save_summary(
        BASE_DIR / config["report_dir"], kind, report_date_text, content
    )
    summary = summary_text_summary(
        report_date_text, kind, enriched, alerts, str(path)
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
    return path, summary, enriched


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    save_config(config)
    database = _db(config)
    database.init()
    example = {
        "as_of": date.today().isoformat(),
        "cash": 0,
        "items": [
            {
                "code": "600519",
                "shares": 100,
                "cost_price": 1500.0,
                "note": "示例，可删除；实际持仓请复制为 holdings.json",
            }
        ],
    }
    example_path = BASE_DIR / config["data_dir"] / "holdings.example.json"
    if not example_path.exists():
        example_path.write_text(
            json.dumps(example, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"股票追踪已初始化。配置：{STOCK_CONFIG_PATH}")
    print(f"数据库：{database.path}")
    print(f"报告目录：{BASE_DIR / config['report_dir']}")
    print(f"持仓示例：{example_path}")
    print()
    print("下一步：")
    print("  1. python3 -m stock_tracker add 600519 --role 核心 --weight 0.10")
    print("  2. python3 -m stock_tracker set-webhook <股票追踪机器人 webhook>")
    print("  3. python3 -m stock_tracker test-notify --webhook-only")
    print("  4. python3 -m stock_tracker run")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    client = _client(config)
    database = _db(config)

    code = str(args.code).strip()
    search_name = ""
    if not is_stock_code(code):
        results = search_stock(client, code)
        if not results:
            print(f"没有找到与“{code}”匹配的 A 股。")
            return 1
        if len(results) > 1:
            print("找到多只股票，请用 6 位代码重新添加：")
            for item in results[:15]:
                print(f"  {item['code']}  {item['name']}  {item.get('market', '')}")
            return 1
        code = results[0]["code"]
        search_name = results[0]["name"]

    try:
        stock_data = fetch_stock(client, code, years=1, announcement_limit=10)
    except Exception as exc:  # noqa: BLE001
        print(f"添加失败：{exc}")
        return 1

    quote = stock_data.get("quote") or {}
    name = quote.get("name") or stock_data.get("name") or search_name or code
    category = args.category or quote.get("industry") or "未分类"
    try:
        weight = _weight(args.weight)
    except ValueError as exc:
        print(str(exc))
        return 1
    database.upsert_stock(
        code=code,
        name=name,
        category=category,
        role=args.role,
        target_weight=weight,
        note=args.note,
    )
    print(f"已添加：{code} {name}（{category}）")
    if weight is None:
        print("未设置目标权重：报告会先把它当观察池，四组里通常只出现“暂不动作”。")
    else:
        print(f"目标权重：{_weight_text(weight)}")
    print("下一步：python3 -m stock_tracker run")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    config = load_config()
    database = _db(config)
    if database.remove_stock(args.code):
        print(f"已移除 {args.code}")
        return 0
    print(f"跟踪列表中没有 {args.code}")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config()
    database = _db(config)
    stocks = database.list_stocks()
    if not stocks:
        print("股票跟踪列表为空。")
        return 0
    rows = []
    for stock in stocks:
        rows.append(
            [
                stock["code"],
                stock["name"],
                stock.get("category") or "—",
                stock.get("role") or "—",
                _weight_text(stock.get("target_weight")),
                stock.get("note") or "",
            ]
        )
    print(_table(["代码", "名称", "行业", "角色", "目标权重", "备注"], rows))
    print(f"\n共 {len(stocks)} 只。")
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    path = holdings_path(config)
    raw = load_holdings(config)
    print(f"持仓文件：{path}")
    if not raw.get("loaded"):
        print("状态：未加载或为空。")
        print()
        print("把下面结构复制到 holdings.json（不要提交/发给别人）：")
        print(
            json.dumps(
                {
                    "as_of": date.today().isoformat(),
                    "cash": 0,
                    "items": [
                        {
                            "code": "600519",
                            "shares": 100,
                            "cost_price": 1500.0,
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    database = _db(config)
    latest = database.latest_price_date()
    rows = database.get_snapshots(latest) if latest else []
    holdings = attach_holdings(rows, raw)
    print(f"状态：已加载 {len(raw.get('items') or [])} 条；现金 {raw.get('cash', 0):,.2f}")
    if holdings.get("total_value"):
        print(f"按最新快照估算总资产：{holdings.get('total_value'):,.2f}")
    unmatched = holdings.get("unmatched") or []
    if unmatched:
        print(
            f"持仓文件中有 {len(unmatched)} 条未在观察池："
            f"{'、'.join(str(code) for code in unmatched)}；"
            "未匹配的个股不会出现在报告动作里。"
        )
    if not rows:
        print("还没有行情快照；先运行 run，才能计算市值和权重。")
        return 0
    table_rows = []
    for row in rows:
        if (row.get("current_value") or 0) <= 0:
            continue
        table_rows.append(
            [
                row.get("code", ""),
                row.get("name", ""),
                _fmt(row.get("shares"), 0),
                _fmt(row.get("cost_price")),
                _fmt(row.get("price")),
                _fmt(row.get("current_value")),
                _fmt(row.get("pnl")),
                _fmt(row.get("pnl_pct")),
                _weight_text(row.get("actual_weight")),
            ]
        )
    if not table_rows:
        print("持仓文件里的代码没有匹配到当前快照。")
        return 0
    print(
        _table(
            ["代码", "名称", "股数", "成本价", "现价", "市值", "盈亏", "盈亏%", "权重"],
            table_rows,
        )
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    client = _client(config)
    code = str(args.code).strip()
    if not is_stock_code(code):
        results = search_stock(client, code)
        if not results:
            print(f"没有找到与“{code}”匹配的 A 股。")
            return 1
        code = results[0]["code"]
        print(f"匹配：{code} {results[0]['name']}")
    try:
        stock_data = fetch_stock(client, code, years=4, announcement_limit=20)
        metrics = build_metrics(stock_data, config)
    except Exception as exc:  # noqa: BLE001
        print(f"检查失败：{exc}")
        return 1

    macro_snapshot: dict = {}
    macro_section = ""
    if args.full:
        macro_snapshot, macro_section = _macro_context(client, config)

    print(f"{metrics['name']}（{metrics['code']}）")
    print(
        _table(
            ["项目", "数值"],
            [
                ["行业", metrics.get("industry") or "—"],
                ["价格", f"{_fmt(metrics.get('price'))}（{metrics.get('price_date')}）"],
                ["当日", f"{_fmt(metrics.get('daily_pct'))}%"],
                ["20日", f"{_fmt(metrics.get('ret_20d'))}%"],
                ["1年", f"{_fmt(metrics.get('ret_250d'))}%"],
                ["近1年回撤", f"{_fmt(metrics.get('dd_1y'))}%"],
                ["近1年波动", f"{_fmt(metrics.get('vol_1y'))}%"],
                ["PE / PB", f"{_fmt(metrics.get('pe'))} / {_fmt(metrics.get('pb'))}"],
                ["市值(亿)", _fmt(metrics.get("market_cap_yi"), 1)],
                ["评分", _fmt(metrics.get("score"), 1)],
                ["质量 / 趋势 / 位置 / 风险", (
                    f"{_fmt(metrics.get('quality_score'), 1)} / "
                    f"{_fmt(metrics.get('trend_score'), 1)} / "
                    f"{_fmt(metrics.get('position_score'), 1)} / "
                    f"{_fmt(metrics.get('risk_score'), 1)}"
                )],
                ["技术趋势", metrics.get("trend_label") or "—"],
            ],
        )
    )
    print("\n质量分项：")
    for name, part in (metrics.get("quality_parts") or {}).items():
        print(
            f"  {name}: {_fmt(part.get('score'), 1)} 分 × {part.get('weight')}%"
        )
    evidence = build_evidence([metrics], macro_snapshot)
    ev = (evidence.get("results") or {}).get(code, {})
    print(
        f"\n证据分：{_fmt(ev.get('score'), 1)}（{ev.get('grade', '—')}）"
        f"；质量 {_fmt(ev.get('quality'), 1)}、趋势 {_fmt(ev.get('trend'), 1)}、"
        f"位置 {_fmt(ev.get('position'), 1)}、宏观 {_fmt(ev.get('macro'), 1)}、"
        f"风险 {_fmt(ev.get('risk'), 1)}"
    )
    alerts = _build_alerts(metrics, None, config)
    alerts.extend(_announcement_alerts(metrics, config))
    print("\n预警：")
    if alerts:
        for alert in alerts:
            print(f"  [{alert['level']}] {alert['message']}")
    else:
        print("  无。")
    announcements = stock_data.get("announcements") or []
    if announcements:
        print("\n最新公告：")
        for item in announcements[:5]:
            print(f"  {item.get('date')}  {item.get('title')}")
    if args.full:
        market = fetch_market(client)
        print("\n指数：")
        for item in market:
            print(f"  {item.get('name')}: {_fmt(item.get('current'))} {_fmt(item.get('pct'))}%")
        if macro_section:
            print("\n" + macro_section)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    if _drop_reused_webhook(config):
        print("检测到股票 webhook 与基金 webhook 相同；为避免误发，已跳过飞书推送，仅生成报告。")
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    stocks = database.list_stocks()
    if not stocks:
        print("股票跟踪列表为空。先运行：python3 -m stock_tracker add 600519 --weight 0.10")
        return 1

    client = _client(config)
    if args.force:
        client.default_cache_hours = 0
        client.force = True

    rows: list[dict] = []
    all_alerts: list[dict] = []
    errors: list[str] = []

    for index, stock in enumerate(stocks, 1):
        code = stock["code"]
        print(f"[{index}/{len(stocks)}] {code} {stock['name']} ...")
        try:
            stock_data = fetch_stock(
                client, code, years=4, announcement_limit=30
            )
            metrics = build_metrics(stock_data, config, today=run_date)
            _attach_meta(metrics, stock)
            database.save_prices(code, stock_data.get("kline") or [])
            announcements = []
            for item in stock_data.get("announcements") or []:
                item_date = str(item.get("date") or "")[:10]
                if item_date and item_date > run_date_text:
                    continue
                announcements.append(item)
            metrics["announcements"] = announcements
            previous = database.get_previous_snapshot(code, run_date_text)
            stock_alerts = _build_alerts(metrics, previous, config)
            stock_alerts.extend(_announcement_alerts(metrics, config, as_of=run_date))
            metrics["alert_count"] = len(stock_alerts)
            database.save_snapshot(code, run_date_text, metrics)
            rows.append(metrics)
            all_alerts.extend(stock_alerts)
            print(
                f"  价格 {metrics['price']:.2f}（{metrics['price_date']}）"
                f" 当日 {_fmt(metrics.get('daily_pct'))}%"
                f" 评分 {metrics['score']:.1f}"
                f" 证据分待算"
            )
        except Exception as exc:  # noqa: BLE001 - one stock should not break the run
            message = f"{code} {stock['name']}: {exc}"
            errors.append(message)
            print(f"  失败：{exc}")
        time.sleep(0.25)

    if not rows:
        summary = f"{run_date_text}：没有可用行情，未生成报告。"
        database.save_run(run_date_text, "failed", summary)
        print(summary)
        if errors:
            print("失败明细：")
            for error in errors:
                print(f"  - {error}")
        return 1

    holdings_info = attach_holdings(rows, load_holdings(config))
    market = fetch_market(client)
    macro_snapshot, macro_section = _macro_context(client, config)
    evidence = build_evidence(rows, macro_snapshot)
    ai_section = _ai_daily_section(
        database,
        config,
        macro_snapshot,
        run_date_text,
    )
    learning_section = _learning_context(database, run_date_text)
    rows.sort(key=lambda item: -(item.get("score") or 0))
    report = generate_daily_report(
        run_date_text,
        rows,
        market,
        all_alerts,
        config,
        macro_section=macro_section,
        evidence=evidence,
        holdings=holdings_info,
        errors=errors,
        learning_section=learning_section,
        ai_section=ai_section,
    )
    report_path = _save_daily(
        BASE_DIR / config["report_dir"], run_date_text, report
    )
    database.save_alerts(run_date_text, all_alerts)
    csv_path = BASE_DIR / config["report_dir"] / f"{run_date_text}.csv"
    database.export_csv(csv_path, run_date_text)
    enriched = enrich_rows(rows, all_alerts, evidence)
    summary = summary_text_daily(
        run_date_text, enriched, all_alerts, str(report_path)
    )
    status = "ok" if not errors else ("partial" if rows else "failed")
    database.save_run(run_date_text, status, summary)

    full_report_mode = _full_report_mode(config)
    if not args.no_notify:
        if full_report_mode == "all":
            notify_report(config, "股票追踪日报", summary, report)
        else:
            notify(config, "股票追踪日报", summary)

    if rows and config.get("review", {}).get("auto", True):
        try:
            review_result = _generate_review_if_due(
                database,
                config,
                client,
                run_date_text,
                macro_snapshot=macro_snapshot,
                macro_section=macro_section,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{_review_label(config)}生成失败：{exc}")
            review_result = None
        if review_result:
            review_path, review_summary, _ = review_result
            print(review_summary)
            print(f"{_review_label(config)}：{review_path}")
            if not args.no_notify:
                if full_report_mode in {"review", "all"}:
                    notify_report(
                        config,
                        f"股票追踪·{_review_label(config)}",
                        review_summary,
                        review_path.read_text(encoding="utf-8"),
                    )
                else:
                    notify(config, f"股票追踪·{_review_label(config)}", review_summary)

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
                    notify_report(
                        config,
                        f"股票追踪·{label}",
                        summary_message,
                        summary_path.read_text(encoding="utf-8"),
                    )
                else:
                    notify(config, f"股票追踪·{label}", summary_message)

    print()
    print(summary)
    print(f"报告：{report_path}")
    print(f"CSV：{csv_path}")
    if errors:
        print(f"\n有 {len(errors)} 只股票失败：")
        for error in errors:
            print(f"  - {error}")
    return 0 if rows else 1


def cmd_review(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    if _drop_reused_webhook(config):
        print("检测到股票 webhook 与基金 webhook 相同；为避免误发，已跳过飞书推送。")
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    client = _client(config)
    label = _review_label(config)
    if not config.get("review", {}).get("enabled", True) and not args.force:
        print(f"{label}已在 stock-config.json 里关闭；要生成请加 --force。")
        return 0
    due, period_start, days, interval, _ = _review_due_info(
        database, run_date_text, config
    )
    if not due and not args.force:
        print(f"还没到推荐日：自 {period_start} 起 {days}/{interval} 个交易日。")
        return 0
    result = _generate_review_if_due(
        database, config, client, run_date_text, force=args.force
    )
    if not result:
        print("没有可用快照，先运行 run。")
        return 1
    path, summary, _ = result
    if not args.no_notify:
        if _full_report_mode(config) in {"review", "all"}:
            notify_report(
                config,
                f"股票追踪·{label}",
                summary,
                path.read_text(encoding="utf-8"),
            )
        else:
            notify(config, f"股票追踪·{label}", summary)
    print(summary)
    print(f"推荐报告：{path}")
    return 0


def _cmd_summary(kind: str, args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    if _drop_reused_webhook(config):
        print("检测到股票 webhook 与基金 webhook 相同；为避免误发，已跳过飞书推送。")
    run_date = _parse_date(args.date)
    run_date_text = run_date.isoformat()
    database = _db(config)
    client = _client(config)
    label = "周度总结" if kind == "weekly" else "月度总结"
    if not config.get("summary", {}).get("enabled", True) and not args.force:
        print(f"{label}已在 stock-config.json 里关闭；要生成请加 --force。")
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
        kind, database, config, client, run_date_text, force=args.force
    )
    if not result:
        print(f"{label}没有生成：还没有可用快照，先运行 run。")
        return 1
    path, summary, _ = result
    if not args.no_notify:
        if _full_report_mode(config) in {"review", "all"}:
            notify_report(
                config,
                f"股票追踪·{label}",
                summary,
                path.read_text(encoding="utf-8"),
            )
        else:
            notify(config, f"股票追踪·{label}", summary)
    print(summary)
    print(f"{label}：{path}")
    return 0


def cmd_weekly(args: argparse.Namespace) -> int:
    return _cmd_summary("weekly", args)


def cmd_monthly(args: argparse.Namespace) -> int:
    return _cmd_summary("monthly", args)


def cmd_set_webhook(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    url = str(args.url).strip()
    if not url.startswith(("http://", "https://")):
        print("webhook 必须是 http/https 地址。")
        return 1
    if _fund_webhook_url() and url == _fund_webhook_url():
        print("这个 webhook 属于基金机器人；股票必须使用单独的“股票追踪”机器人。")
        return 1
    config.setdefault("notify", {})
    config["notify"]["webhook_url"] = url
    if args.secret is not None:
        config["notify"]["webhook_secret"] = str(args.secret).strip()
    save_config(config)
    print("已保存股票追踪机器人 webhook（不显示完整地址）。")
    if is_feishu_webhook(url):
        print("识别为飞书自定义机器人；完整版会按卡片发送。")
    else:
        print("未识别为飞书 /hook/ 地址，会按通用 webhook 发送。")
    print("下一步：python3 -m stock_tracker test-notify --webhook-only")
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    config = load_config()
    ensure_dirs(config)
    if args.webhook_only:
        config.setdefault("notify", {})
        config["notify"]["macos"] = False
    notify_config = config.get("notify", {})
    webhook = str(notify_config.get("webhook_url") or "").strip()
    if _webhook_reuses_fund(config):
        print("这个 webhook 属于基金机器人；股票必须使用单独的“股票追踪”机器人。")
        return 1
    if not webhook and not notify_config.get("macos"):
        print("既没有 webhook，也关闭了 macOS 通知；先 set-webhook。")
        return 1
    content = "\n".join(
        [
            "# 股票追踪 · 通知测试",
            "",
            "> 这是一条测试消息，用于确认“股票追踪”机器人能收到卡片。",
            "",
            "## 测试内容",
            "",
            "| 项目 | 内容 |",
            "|---|---|",
            f"| 发送时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |",
            "| 机器人 | 股票追踪 |",
            "| 报告类型 | 测试卡片 |",
            "",
            "## 说明",
            "",
            "- 收到这条消息，说明 webhook 和飞书卡片通道正常。",
            "- 正式报告会以“股票追踪日报 / 2日推荐 / 周度总结 / 月度总结”开头。",
            "- 本工具不能查看账户、不能代下单、不能保证收益。",
            "",
            "---",
            "",
            f"_生成时间：{date.today().isoformat()} · stock-tracker v0.1.0_",
            "",
        ]
    )
    ok = notify_report(config, "股票追踪 · 通知测试", "股票追踪机器人测试消息", content)
    if ok and webhook:
        print("发送成功：已尝试 macOS 和股票追踪飞书机器人。")
    elif ok:
        print("已发送 macOS 通知；尚未配置股票追踪飞书 webhook。")
    else:
        print("发送失败：检查 webhook/secret/网络。")
    return 0 if ok else 1


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
    config = load_config()
    (LAUNCHD_DIR / config["data_dir"]).mkdir(parents=True, exist_ok=True)
    wrapper = LAUNCHD_DIR / "run_daily.sh"
    python_bin = Path(sys.executable).resolve()
    wrapper.write_text(
        "#!/bin/zsh\n"
        f'cd "{BASE_DIR}" || exit 1\n'
        f'PYTHON_BIN="{python_bin}"\n'
        'if [ ! -x "$PYTHON_BIN" ]; then\n'
        '  PYTHON_BIN="$(command -v python3)"\n'
        "fi\n"
        f"exec >> {config['data_dir']}/cron.log 2>&1\n"
        'exec "$PYTHON_BIN" -m stock_tracker run "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return LAUNCHD_DIR


def _plist_content(config: dict, launchd_dir: Path | None = None) -> str:
    hour = int(config.get("schedule", {}).get("hour", 22))
    minute = int(config.get("schedule", {}).get("minute", 40))
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
        print("已卸载股票追踪定时任务。")
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
        minute = config.get("schedule", {}).get("minute", 40)
        print(f"已安装：每个工作日 {hour:02d}:{minute:02d} 自动运行股票追踪")
        print(f"plist：{plist_path}")
        print("查看状态：python3 -m stock_tracker schedule --status")
        return 0

    print("请指定 --install / --uninstall / --status")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-tracker",
        description="本地 A 股跟踪工具：抓公开行情/财务/公告，生成股票四组推荐。",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化股票目录、配置和数据库")
    init_parser.set_defaults(func=cmd_init)

    add_parser = subparsers.add_parser("add", help="添加股票（代码或名称关键词）")
    add_parser.add_argument("code", help="6 位 A 股代码，或名称关键词")
    add_parser.add_argument("--category", help="手动指定行业/类别；默认用行情行业")
    add_parser.add_argument("--role", default="观察", help="角色：核心/卫星/观察")
    add_parser.add_argument("--weight", type=float, help="目标权重，例如 0.10 或 10")
    add_parser.add_argument("--note", help="备注")
    add_parser.set_defaults(func=cmd_add)

    remove_parser = subparsers.add_parser("remove", help="移除股票")
    remove_parser.add_argument("code")
    remove_parser.set_defaults(func=cmd_remove)

    list_parser = subparsers.add_parser("list", help="查看股票跟踪列表")
    list_parser.set_defaults(func=cmd_list)

    holdings_parser = subparsers.add_parser("holdings", help="查看实际持仓文件状态")
    holdings_parser.set_defaults(func=cmd_holdings)

    run_parser = subparsers.add_parser(
        "run",
        help="抓取行情并生成日报；到期自动生成 2 日/周/月报告",
    )
    run_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    run_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    run_parser.add_argument("--force", action="store_true", help="忽略缓存，强制重新抓取")
    run_parser.set_defaults(func=cmd_run)

    review_parser = subparsers.add_parser(
        "review",
        help="生成 2 日股票推荐与参考区间",
    )
    review_parser.add_argument("--date", help="指定日期 YYYY-MM-DD")
    review_parser.add_argument("--force", action="store_true", help="不到期也强制生成")
    review_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    review_parser.set_defaults(func=cmd_review)

    weekly_parser = subparsers.add_parser("weekly", help="生成周五股票周度总结")
    weekly_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    weekly_parser.add_argument("--force", action="store_true", help="不是周五也强制生成")
    weekly_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    weekly_parser.set_defaults(func=cmd_weekly)

    monthly_parser = subparsers.add_parser(
        "monthly", help="生成每月最后一个工作日的股票月度总结"
    )
    monthly_parser.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天")
    monthly_parser.add_argument("--force", action="store_true", help="不是月末工作日也强制生成")
    monthly_parser.add_argument("--no-notify", action="store_true", help="不发送通知")
    monthly_parser.set_defaults(func=cmd_monthly)

    check_parser = subparsers.add_parser("check", help="检查单只股票的数据和评分")
    check_parser.add_argument("code", help="6 位代码或名称关键词")
    check_parser.add_argument("--full", action="store_true", help="同时抓指数和宏观")
    check_parser.set_defaults(func=cmd_check)

    webhook_parser = subparsers.add_parser(
        "set-webhook", help="保存“股票追踪”机器人的 webhook"
    )
    webhook_parser.add_argument("url", help="飞书自定义机器人 webhook")
    webhook_parser.add_argument("--secret", help="飞书签名密钥（可选）")
    webhook_parser.set_defaults(func=cmd_set_webhook)

    test_notify_parser = subparsers.add_parser(
        "test-notify", help="发送一条股票追踪测试消息"
    )
    test_notify_parser.add_argument(
        "--webhook-only",
        action="store_true",
        help="只测试 webhook/飞书，不弹 macOS 通知",
    )
    test_notify_parser.set_defaults(func=cmd_test_notify)

    schedule_parser = subparsers.add_parser(
        "schedule", help="安装/卸载股票追踪 macOS 定时任务"
    )
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
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001
        logging.exception("命令失败")
        print(f"命令失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
