from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable, Sequence

from shared.trading_calendar import next_weekday_date as _core_next_weekday_date
from shared.trading_calendar import next_trading_date

HORIZONS = (2, 5, 20)

LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    action TEXT,
    group_name TEXT,
    stance TEXT NOT NULL,
    target_weight REAL,
    price REAL,
    score REAL,
    evidence REAL,
    reason TEXT,
    thesis TEXT,
    horizon_days INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    outcome_price REAL,
    outcome_date TEXT,
    outcome_return REAL,
    max_adverse REAL,
    outcome_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    evaluated_at TEXT,
    UNIQUE(report_date, report_kind, code, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_predictions_due
    ON predictions(outcome_status, due_date);
CREATE INDEX IF NOT EXISTS idx_predictions_code_date
    ON predictions(code, report_date);
"""

STATUS_LABELS = {
    "hit": "命中",
    "miss": "未中",
    "flat": "走平",
    "neutral": "观察",
    "pending": "待评估",
}


def ensure_learning_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(LEARNING_SCHEMA)


def next_weekday_date(start_date: str, trading_days: int) -> str:
    """Deprecated：仅周末近似。新代码用 shared.trading_calendar.next_trading_date。"""
    return _core_next_weekday_date(start_date, trading_days)


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stance_for_action(action: str | None) -> str:
    """Map the report action to a testable directional stance.

    The learning log only scores directional calls. 观察/复核 are recorded as
    neutral because they are not a buy or sell call; they are not counted in
    hit-rate statistics.
    """
    text = str(action or "")
    if any(word in text for word in ("买入", "持有", "试探")):
        return "bullish"
    if any(word in text for word in ("减仓", "出货", "剔除", "止盈")):
        return "bearish"
    return "neutral"


def record_predictions(
    db_path: Path | str,
    report_date: str,
    report_kind: str,
    rows: Iterable[dict],
    *,
    price_key: str = "price",
    horizons: Sequence[int] = HORIZONS,
    module_label: str = "",
) -> int:
    """Append the current actionable calls to the local prediction log.

    Rows are idempotent per (report_date, report_kind, code, horizon).  A
    forced re-run updates the call text and starting price but deliberately
    keeps any outcome that has already been filled in.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    with sqlite3.connect(str(path)) as connection:
        ensure_learning_schema(connection)
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            action = str(
                row.get("review_action") or row.get("action") or ""
            ).strip()
            if not action:
                continue
            price = _as_float(row.get(price_key))
            if price is None or price <= 0:
                continue
            name = str(row.get("name") or code)
            stance = stance_for_action(action)
            evidence = row.get("evidence") or {}
            evidence_score = (
                _as_float(evidence.get("score"))
                if isinstance(evidence, dict)
                else None
            )
            score = _as_float(row.get("score"))
            target_weight = _as_float(row.get("target_weight"))
            reason = str(
                row.get("review_reason") or row.get("reason") or ""
            ).strip()
            thesis = str(
                row.get("note") or row.get("role") or ""
            ).strip()
            for horizon in horizons:
                horizon_days = int(horizon)
                if horizon_days <= 0:
                    continue
                due_date = next_trading_date(report_date, horizon_days)
                connection.execute(
                    """
                    INSERT INTO predictions(
                        report_date, report_kind, code, name, action,
                        group_name, stance, target_weight, price, score,
                        evidence, reason, thesis, horizon_days, due_date,
                        outcome_status, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT(report_date, report_kind, code, horizon_days)
                    DO UPDATE SET
                        name=excluded.name,
                        action=excluded.action,
                        group_name=excluded.group_name,
                        stance=excluded.stance,
                        target_weight=excluded.target_weight,
                        price=excluded.price,
                        score=excluded.score,
                        evidence=excluded.evidence,
                        reason=excluded.reason,
                        thesis=excluded.thesis,
                        due_date=excluded.due_date
                    """,
                    (
                        report_date,
                        report_kind,
                        code,
                        name,
                        action,
                        str(row.get("recommendation_group") or ""),
                        stance,
                        target_weight,
                        price,
                        score,
                        evidence_score,
                        reason,
                        thesis,
                        horizon_days,
                        due_date,
                        now,
                    ),
                )
                count += 1
    return count


def _normalise_series(raw: Iterable) -> list[tuple[str, float]]:
    series: list[tuple[str, float]] = []
    for item in raw or []:
        if isinstance(item, dict):
            day = item.get("date")
            value = item.get("close")
            if value is None:
                value = item.get("nav")
            if value is None:
                value = item.get("value")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            day, value = item[0], item[1]
        else:
            continue
        day_text = str(day or "")
        number = _as_float(value)
        if day_text and number is not None and number > 0:
            series.append((day_text, number))
    series.sort(key=lambda pair: pair[0])
    return series


def evaluate_predictions(
    db_path: Path | str,
    as_of: str,
    series_loader: Callable[[str, str, str], Iterable],
) -> int:
    """Fill in 2/5/20-day outcomes that are due as of ``as_of``.

    ``series_loader(code, start_date, end_date)`` must return an iterable of
    dicts/tuples with a date and a positive price or NAV.  No network access is
    performed here.
    """
    path = Path(db_path)
    as_of_text = str(as_of)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    evaluated = 0
    with sqlite3.connect(str(path)) as connection:
        connection.row_factory = sqlite3.Row
        ensure_learning_schema(connection)
        rows = connection.execute(
            """
            SELECT * FROM predictions
            WHERE outcome_status='pending' AND due_date<=?
            ORDER BY report_date, code, horizon_days
            """,
            (as_of_text,),
        ).fetchall()
        cache: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for prediction in rows:
            cache_key = (prediction["code"], prediction["report_date"])
            if cache_key not in cache:
                try:
                    raw = series_loader(
                        prediction["code"],
                        prediction["report_date"],
                        as_of_text,
                    )
                except Exception:  # noqa: BLE001 - one bad series should not stop the log
                    raw = []
                cache[cache_key] = _normalise_series(raw)
            series = cache[cache_key]
            if not series:
                continue
            due_date = prediction["due_date"]
            end_point = next(
                ((day, value) for day, value in series if day >= due_date),
                None,
            )
            if end_point is None:
                continue
            outcome_date, outcome_price = end_point
            start_price = _as_float(prediction["price"])
            if start_price is None or start_price <= 0:
                start_price = series[0][1]
            if start_price <= 0:
                continue
            interval = [
                (day, value) for day, value in series if day <= outcome_date
            ]
            returns = [
                (value / start_price - 1.0) * 100.0
                for _, value in interval
            ]
            outcome_return = (outcome_price / start_price - 1.0) * 100.0
            stance = prediction["stance"]
            if stance == "bearish":
                max_adverse = max(returns) if returns else 0.0
            else:
                max_adverse = min(returns) if returns else 0.0

            if stance == "neutral":
                status = "neutral"
            elif abs(outcome_return) < 0.05:
                status = "flat"
            elif (stance == "bullish" and outcome_return > 0) or (
                stance == "bearish" and outcome_return < 0
            ):
                status = "hit"
            else:
                status = "miss"

            connection.execute(
                """
                UPDATE predictions SET
                    outcome_price=?, outcome_date=?, outcome_return=?,
                    max_adverse=?, outcome_status=?, evaluated_at=?
                WHERE id=?
                """,
                (
                    outcome_price,
                    outcome_date,
                    round(outcome_return, 4),
                    round(max_adverse, 4),
                    status,
                    now,
                    prediction["id"],
                ),
            )
            evaluated += 1
    return evaluated


def _empty_bucket() -> dict:
    return {
        "total": 0,
        "evaluated": 0,
        "hit": 0,
        "miss": 0,
        "flat": 0,
        "neutral": 0,
        "pending": 0,
        "directional": 0,
        "hit_rate": None,
        "avg_return": None,
        "avg_adverse": None,
        "neutral_evaluated": 0,
        "neutral_down": 0,
        "neutral_avoid_rate": None,
        "neutral_avg_return": None,
    }


def summarize_predictions(
    db_path: Path | str,
    *,
    as_of: str | None = None,
    report_kind: str | None = "review",
    module_label: str = "",
) -> dict:
    """Return a small, auditable scorecard for the prediction log."""
    path = Path(db_path)
    with sqlite3.connect(str(path)) as connection:
        connection.row_factory = sqlite3.Row
        ensure_learning_schema(connection)
        clauses: list[str] = []
        params: list[str] = []
        if report_kind:
            clauses.append("report_kind=?")
            params.append(report_kind)
        if as_of:
            clauses.append("report_date<=?")
            params.append(str(as_of))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM predictions {where} ORDER BY report_date DESC, id DESC",
            params,
        ).fetchall()

    buckets: dict[int, dict] = {}
    returns_by_horizon: dict[int, list[float]] = {}
    adverse_by_horizon: dict[int, list[float]] = {}
    neutral_returns_by_horizon: dict[int, list[float]] = {}
    evaluated_items: list[dict] = []
    new_count = 0
    for prediction in rows:
        horizon = int(prediction["horizon_days"])
        bucket = buckets.setdefault(horizon, _empty_bucket())
        bucket["total"] += 1
        if as_of and prediction["report_date"] == str(as_of):
            new_count += 1
        status = prediction["outcome_status"] or "pending"
        if status in bucket:
            bucket[status] += 1
        else:
            bucket["pending"] += 1
        if status in {"hit", "miss", "flat", "neutral"}:
            bucket["evaluated"] += 1
            outcome_return = _as_float(prediction["outcome_return"])
            max_adverse = _as_float(prediction["max_adverse"])
            if outcome_return is not None:
                returns_by_horizon.setdefault(horizon, []).append(outcome_return)
                if status == "neutral":
                    bucket["neutral_evaluated"] += 1
                    if outcome_return <= 0:
                        bucket["neutral_down"] += 1
                    neutral_returns_by_horizon.setdefault(horizon, []).append(
                        outcome_return
                    )
            if max_adverse is not None:
                adverse_by_horizon.setdefault(horizon, []).append(max_adverse)
            evaluated_items.append(dict(prediction))
        if status in {"hit", "miss"}:
            bucket["directional"] += 1

    for horizon, bucket in buckets.items():
        if bucket["directional"]:
            bucket["hit_rate"] = bucket["hit"] / bucket["directional"] * 100.0
        returns = returns_by_horizon.get(horizon) or []
        adverse = adverse_by_horizon.get(horizon) or []
        if returns:
            bucket["avg_return"] = mean(returns)
        if adverse:
            bucket["avg_adverse"] = mean(adverse)
        neutral_returns = neutral_returns_by_horizon.get(horizon) or []
        if neutral_returns:
            bucket["neutral_avg_return"] = mean(neutral_returns)
            bucket["neutral_avoid_rate"] = (
                bucket["neutral_down"] / len(neutral_returns) * 100.0
            )

    evaluated_items.sort(
        key=lambda item: (
            str(item.get("evaluated_at") or ""),
            str(item.get("report_date") or ""),
        ),
        reverse=True,
    )
    recent = evaluated_items[:5]

    def return_key(item: dict, default: float) -> float:
        value = _as_float(item.get("outcome_return"))
        return default if value is None else value

    best = max(
        evaluated_items,
        key=lambda item: return_key(item, -9999),
        default=None,
    )
    worst = min(
        evaluated_items,
        key=lambda item: return_key(item, 9999),
        default=None,
    )
    return {
        "module": module_label,
        "total": len(rows),
        "new_count": new_count,
        "pending": sum(
            1 for item in rows if (item["outcome_status"] or "pending") == "pending"
        ),
        "horizons": buckets,
        "recent": recent,
        "best": best,
        "worst": worst,
    }


def _fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.2f}%"


def _item_label(item: dict | None) -> str:
    if not item:
        return "—"
    return (
        f"{item.get('name') or item.get('code')}（{item.get('code')}）"
        f"·{item.get('action') or '—'}·{int(item.get('horizon_days') or 0)}日"
    )


def render_learning_section(summary: dict, module_label: str = "") -> str:
    module = summary.get("module") or module_label or "组合"
    total = int(summary.get("total") or 0)
    new_count = int(summary.get("new_count") or 0)
    pending = int(summary.get("pending") or 0)
    lines = [
        "## 学习与复盘（自动）",
        "",
        "> 只记录当时动作，2/5/20 个交易日后按价格/净值回填；"
        "不自动改评分、目标权重或下单。单个周期方向样本少于 20 条时只观察。"
        "样本按预测条数计，2/5/20 日窗口高度重叠，不等于独立样本。",
        "",
    ]
    if total == 0:
        lines.extend(
            [
                "- 尚无预测记录。下一次 2 日推荐生成后开始登记。",
                "",
            ]
        )
        return "\n".join(lines)

    lines.append(
        f"- {module}预测已登记 **{total}** 条；报告日新增 **{new_count}** 条；"
        f"待评估 **{pending}** 条。"
    )
    lines.append("")
    lines.extend(
        [
            "| 周期 | 已评估 | 方向命中 | 未中 | 走平 | 观察 | 待评估 | 方向命中率 | 平均涨跌 | 平均最大不利 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for horizon in sorted(summary.get("horizons") or {}):
        bucket = summary["horizons"][horizon]
        hit_rate = (
            f"{bucket['hit_rate']:.0f}%"
            if bucket.get("hit_rate") is not None
            else "—"
        )
        lines.append(
            f"| {horizon} 日 | {bucket.get('evaluated', 0)} | {bucket.get('hit', 0)} | "
            f"{bucket.get('miss', 0)} | {bucket.get('flat', 0)} | "
            f"{bucket.get('neutral', 0)} | {bucket.get('pending', 0)} | "
            f"{hit_rate} | {_fmt_pct(bucket.get('avg_return'))} | "
            f"{_fmt_pct(bucket.get('avg_adverse'))} |"
        )
    lines.append("")
    for horizon in sorted(summary.get("horizons") or {}):
        bucket = summary["horizons"][horizon]
        if not bucket.get("neutral_evaluated"):
            continue
        lines.append(
            f"- {horizon} 日观察类（不计命中率）：已评估 {bucket['neutral_evaluated']} 条，"
            f"其中 {bucket['neutral_down']} 条期间下跌/走平"
            f"（{bucket['neutral_avoid_rate']:.0f}%），"
            f"平均涨跌 {_fmt_pct(bucket.get('neutral_avg_return'))}。"
        )
    if any(
        bucket.get("neutral_evaluated")
        for bucket in (summary.get("horizons") or {}).values()
    ):
        lines.append("")

    recent = summary.get("recent") or []
    if recent:
        lines.append("**最近到期**")
        for item in recent:
            status = STATUS_LABELS.get(
                str(item.get("outcome_status") or "pending"), "待评估"
            )
            lines.append(
                f"- {item.get('outcome_date') or '—'} · {_item_label(item)}："
                f"**{_fmt_pct(_as_float(item.get('outcome_return')))}**"
                f"（{status}；最大不利 "
                f"{_fmt_pct(_as_float(item.get('max_adverse')))}）"
            )
        lines.append("")

    best = summary.get("best")
    worst = summary.get("worst")
    if best and worst:
        lines.append("**我错在哪（自动提示）**")
        lines.append(
            f"- 目前最好：**{_item_label(best)}** "
            f"{_fmt_pct(_as_float(best.get('outcome_return')))}。"
        )
        lines.append(
            f"- 目前最差：**{_item_label(worst)}** "
            f"{_fmt_pct(_as_float(worst.get('outcome_return')))}。"
        )
        lines.append(
            "- 复盘顺序：先分清是逻辑错（基本面/估值判断错）、"
            "执行错（追高/仓位过大），还是运气（短期波动）；"
            "下一期只改能验证的那一项。"
        )
        lines.append("")

    directional = max(
        (
            int(bucket.get("directional") or 0)
            for bucket in (summary.get("horizons") or {}).values()
        ),
        default=0,
    )
    if directional < 20:
        lines.append(
            "> 结论：单个周期的方向样本不足 20 条，只记录、不调权重；"
            "先看“最大不利”是否超出你的回撤承受力。"
        )
    else:
        lines.append(
            "> 结论：已有单个周期方向样本达到 20 条，仍先做滚动验证；"
            "不因短期命中率自动改目标权重。"
        )
    lines.append("")
    return "\n".join(lines)
