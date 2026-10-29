from __future__ import annotations

from datetime import date
from typing import Any

from fund_tracker.macro import classify_regime
from shared.trading_calendar import next_month_last_trading_day, next_trading_date

from .evidence import evidence_for

LEVEL_LABEL = {"high": "高", "warn": "中", "info": "提示"}

RECOMMENDATION_GROUPS = [
    "继续持有",
    "可以跟进持有",
    "可以调整",
    "可以出货",
    "暂不动作",
]

GROUP_LABELS = {
    "继续持有": "继续持有",
    "可以跟进持有": "可以跟进持有",
    "可以调整": "可以调整",
    "可以出货": "可以出货",
    "暂不动作": "暂不动作（观察）",
}

GROUP_HINTS = {
    "继续持有": "已到计划仓位，维持；不追涨，单票不超过计划权重。",
    "可以跟进持有": "未建满或未持有，才按参考区间分 2–3 批；单票先建 1/3。",
    "可以调整": "先减到目标或暂停加仓，核对公告/业绩；不是一次清仓。",
    "可以出货": "按预警/止盈分批减，先减 1/3；基本面恶化再考虑清仓。",
    "暂不动作": "不建仓、不追高；等回调、趋势和证据分同时确认。",
}

SEVERE_ALERT_WORDS = (
    "立案",
    "退市",
    "处罚",
    "减持",
    "质押",
    "预亏",
    "亏损",
    "调查",
    "冻结",
    "违规",
    "风险提示",
    "终止",
    "诉讼",
    "问询",
    "监管",
    "ST",
)


from shared.format_utils import ACTION_ORDER, fmt_pct, fmt_num, _weight_text, _period_return

def fmt_money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:,.2f}"


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def _score_delta(row: dict) -> float | None:
    value = row.get("score_delta_window")
    if value is not None:
        return value
    return row.get("score_delta_5d")


def _period_text(row: dict) -> str:
    label = row.get("period_label")
    if label:
        return str(label)
    return f"{int(row.get('period_days') or 2)} 个交易日"

def _evidence_for(row: dict, evidence: dict | None) -> dict:
    return evidence_for(row, evidence)


def _buy_zone(row: dict) -> tuple[float | None, float | None]:
    weight = row.get("target_weight") or 0
    if weight <= 0:
        return None, None
    low = row.get("range_low_1y")
    high = row.get("range_high_1y")
    if low is None or high is None or high <= low:
        price = row.get("price")
        if price is None:
            return None, None
        return float(price) * 0.95, float(price) * 0.98
    buy_low = float(low) + 0.20 * (float(high) - float(low))
    buy_high = float(low) + 0.40 * (float(high) - float(low))
    return buy_low, buy_high


def _sell_zone(row: dict) -> tuple[float | None, float | None]:
    weight = row.get("target_weight") or 0
    if weight <= 0:
        return None, None
    low = row.get("range_low_1y")
    high = row.get("range_high_1y")
    if low is None or high is None or high <= low:
        return None, None
    sell_low = float(low) + 0.80 * (float(high) - float(low))
    return sell_low, float(high)


def _buy_range(row: dict) -> str:
    low, high = _buy_zone(row)
    if low is None or high is None:
        return "—（观察，不建仓）"
    price = row.get("price")
    if price is None:
        return f"{low:.2f}–{high:.2f}（等回到区间；不追高）"
    current = float(price)
    if low <= current <= high:
        return f"{low:.2f}–{high:.2f}（当前已在区间；未建仓分 2–3 批）"
    if current < low:
        return f"{low:.2f}–{high:.2f}（低于区间；先等企稳，不接飞刀）"
    return f"{low:.2f}–{high:.2f}（等回到区间；不追高）"


def _sell_range(row: dict) -> str:
    weight = row.get("target_weight") or 0
    if weight <= 0:
        return "—（观察）"
    sell_low, sell_high = _sell_zone(row)
    price = row.get("price")
    if sell_low is None or sell_high is None or price is None:
        return "—"
    actual = row.get("actual_weight")
    held = actual is not None and float(actual) > 0
    if held:
        text = (
            f"{sell_low:.2f}–{sell_high:.2f}（进入止盈区，分批减 1/3）"
            if float(price) >= sell_low
            else f"{sell_low:.2f}–{sell_high:.2f}（未到，不操作）"
        )
        cost = row.get("cost_price")
        if cost is not None and float(cost) > 0:
            text += f"；风控线 {float(cost) * 0.88:.2f}"
        return text
    if float(price) >= sell_low:
        return f"{sell_low:.2f}–{sell_high:.2f}（已在止盈区；未持有不追高）"
    return f"{sell_low:.2f}–{sell_high:.2f}（未持有；等建仓后再看）"


def _recommendation_bucket(row: dict, action: str, evidence: dict | None) -> str:
    target = float(row.get("target_weight") or 0)
    actual = row.get("actual_weight")
    ev_score = (evidence or {}).get("score")
    if action in {"止盈/减仓", "出货/复核"}:
        if actual is not None and float(actual) > 0:
            return "可以出货"
        return "暂不动作"
    if action in {"减仓/复核", "剔除"}:
        if actual is not None and float(actual) > 0:
            return "可以出货" if target <= 0 else "可以调整"
        return "暂不动作"
    if action == "复核":
        if actual is not None and float(actual) > 0:
            return "可以调整"
        return "暂不动作"
    if action == "观察":
        if actual is not None and float(actual) > 0:
            return "可以调整"
        return "暂不动作"
    if action in {"小仓试探", "分批买入/持有"}:
        return "可以跟进持有"
    if action == "持有":
        if target > 0 and ev_score is not None and ev_score < 70:
            return "可以调整"
        return "继续持有"
    return "暂不动作"


def _review_action(
    row: dict,
    alerts: list[dict],
    evidence: dict | None = None,
) -> tuple[str, str]:
    target = float(row.get("target_weight") or 0)
    actual = row.get("actual_weight")
    score = row.get("score")
    ev_score = (evidence or {}).get("score")
    period_text = _period_text(row)
    ret_period = _period_return(row)
    high_alerts = [alert for alert in alerts if alert.get("level") == "high"]
    severe_alerts = [
        alert
        for alert in high_alerts
        if any(
            word in str(alert.get("message", ""))
            for word in SEVERE_ALERT_WORDS
        )
    ]
    severe_text = "；".join(
        str(alert.get("message", "")) for alert in severe_alerts
    )

    # Real holding path: only here can "继续持有/减仓" be a real position action.
    if actual is not None and float(actual) > 0:
        if target <= 0:
            return "剔除", "目标权重为 0；按纪律清仓/换股，不补仓"
        if severe_alerts:
            return "出货/复核", f"重大预警：{severe_text}"
        if score is None:
            return "复核", "数据不足，先核对公告和行情"
        sell_low, _ = _sell_zone(row)
        if sell_low is not None and row.get("price") is not None and float(row["price"]) >= sell_low:
            return (
                "止盈/减仓",
                f"价格 {row['price']:.2f} 已进入近1年高位止盈区；分批减 1/3，不一次清仓",
            )
        if score < 45:
            return (
                "出货/复核",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "低于清仓复核线，先减 1/3 并核对公告",
            )
        if score < 55 or (ev_score is not None and ev_score < 55):
            return (
                "减仓/复核",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "低于防守线，先减 1/3 或暂停加仓",
            )
        excess = float(actual) - target
        if excess > max(0.02, target * 0.25):
            return (
                "减仓/复核",
                f"实际权重 {_weight_text(actual)} 高于目标 {_weight_text(target)}；先再平衡",
            )
        if (
            target > 0
            and float(actual) < target * 0.70
            and score >= 75
            and (ev_score is None or ev_score >= 70)
        ):
            return (
                "分批买入/持有",
                f"低于目标权重，评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；按区间补",
            )
        if score >= 80 and (ev_score is None or ev_score >= 75):
            return (
                "持有",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；维持目标权重",
            )
        if score >= 70 and (ev_score is None or ev_score >= 65):
            return (
                "持有",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "维持目标权重，暂不加重",
            )
        if ev_score is not None and ev_score < 65:
            return (
                "复核",
                f"证据分 {ev_score:.1f} 偏低；先核对公告，必要时减 1/3",
            )
        return "观察", f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；暂不加重"

    # Watchlist / target-weight path: never pretend a position exists.
    if target <= 0:
        return "观察", "不占仓；等评分、证据分、回调和趋势同时确认"
    if severe_alerts:
        return "复核", f"重大预警：{severe_text}"
    if score is None:
        return "观察", "数据不足"
    if score < 55 or (ev_score is not None and ev_score < 55):
        return "观察", f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；不建仓"
    buy_low, buy_high = _buy_zone(row)
    in_zone = (
        buy_low is not None
        and buy_high is not None
        and row.get("price") is not None
        and buy_low <= float(row["price"]) <= buy_high
    )
    if score >= 80 and (ev_score is None or ev_score >= 75):
        if in_zone:
            return (
                "分批买入/持有",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "价格在参考区间，分 2–3 批",
            )
        return "观察", "证据强，但价格不在参考买入区间；不追高，等回调"
    if score >= 70 and (ev_score is None or ev_score >= 65):
        if in_zone:
            return (
                "分批买入/持有",
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "只小仓分批，不一次买满",
            )
        return "观察", "评分中上，但价格不在参考买入区间；等回调"
    if ret_period is not None and ret_period <= -8 and score >= 60 and (ev_score or 0) >= 60:
        return "观察", f"{period_text}回撤 {fmt_pct(ret_period)}；只观察，不接飞刀"
    return "观察", f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；暂不加重"


def _enrich_recommendations(
    rows: list[dict],
    alerts_by_code: dict[str, list[dict]],
    evidence: dict | None,
) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        item_evidence = _evidence_for(item, evidence)
        action, reason = _review_action(
            item,
            alerts_by_code.get(str(item.get("code") or ""), []),
            item_evidence,
        )
        item["evidence"] = item_evidence
        item["review_action"] = action
        item["review_reason"] = reason
        item["recommendation_group"] = _recommendation_bucket(
            item, action, item_evidence
        )
        item["buy_range"] = _buy_range(item)
        item["sell_range"] = _sell_range(item)
        enriched.append(item)
    enriched.sort(
        key=lambda item: (
            ACTION_ORDER.get(item.get("review_action"), 9),
            -(item.get("score") or 0),
        )
    )
    return enriched


def _group_counts(rows: list[dict]) -> dict[str, int]:
    return {
        group: sum(1 for row in rows if row.get("recommendation_group") == group)
        for group in RECOMMENDATION_GROUPS
    }


def _group_section(rows: list[dict], heading: str = "## 四、四组结论") -> str:
    counts = _group_counts(rows)
    lines = [heading, ""]
    for group in RECOMMENDATION_GROUPS:
        items = [row for row in rows if row.get("recommendation_group") == group]
        label = GROUP_LABELS.get(group, group)
        if not items:
            lines.append(f"- **{label}（0 只）**：本期没有。")
            continue
        names = "、".join(
            f"{row.get('name')}（{row.get('code')}）" for row in items
        )
        lines.append(
            f"- **{label}（{counts[group]} 只）**：{names}。"
            f"{GROUP_HINTS.get(group, '')}"
        )
    lines.append("")
    return "\n".join(lines)


def _group_checklist(rows: list[dict], heading: str = "## 七、执行清单") -> str:
    lines = [heading, ""]
    for group in RECOMMENDATION_GROUPS:
        items = [row for row in rows if row.get("recommendation_group") == group]
        label = GROUP_LABELS.get(group, group)
        if not items:
            lines.append(f"- [ ] **{label}**：本期没有。")
            continue
        names = "、".join(
            f"{row.get('name')}（{row.get('code')}）" for row in items
        )
        lines.append(
            f"- [ ] **{label}**：{names}。{GROUP_HINTS.get(group, '')}"
        )
    lines.append("")
    return "\n".join(lines)


def _market_table(market: list[dict]) -> str:
    if not market:
        return "_行情接口暂不可用。_\n"
    lines = ["| 指数 | 最新 | 当日涨跌 |", "|---|---:|---:|"]
    for item in market:
        lines.append(
            f"| {_cell(item.get('name') or item.get('symbol'))} | "
            f"{fmt_num(item.get('current'))} | {fmt_pct(item.get('pct'))} |"
        )
    return "\n".join(lines) + "\n"


def _period_market_table(market: list[dict], period_label: str) -> str:
    if not market:
        return "_行情接口暂不可用。_\n"
    lines = [
        f"| 指数 | 最新 | {period_label}涨跌 | 当日涨跌 |",
        "|---|---:|---:|---:|",
    ]
    for item in market:
        change = item.get("pct_window")
        if change is None:
            change = item.get("pct_5d")
        lines.append(
            f"| {_cell(item.get('name') or item.get('symbol'))} | "
            f"{fmt_num(item.get('current'))} | {fmt_pct(change)} | {fmt_pct(item.get('pct'))} |"
        )
    return "\n".join(lines) + "\n"


def _macro_read(item: str, macro: dict) -> str:
    china = macro.get("china") or {}
    glob = macro.get("global") or {}
    regime = classify_regime(macro)
    labels = regime.get("labels") or []
    if item == "cn10":
        value = china.get("10y")
        if value is None:
            return "数据不足"
        return "低利率，红利/现金流相对有支撑" if value < 1.8 else "利率偏高，高估值承压"
    if item == "us10":
        value = glob.get("us_10y")
        if value is None:
            return "数据不足"
        return "海外高利率，压制高 PE 成长" if value >= 4.0 else "海外利率回落，估值压力减轻"
    if item == "usdcny":
        value = (glob.get("usdcny") or {}).get("current")
        if value is None:
            return "数据不足"
        return "人民币偏弱，外资敏感板块波动大" if value > 7.05 else "人民币偏稳"
    if item == "oil":
        value = (glob.get("oil") or {}).get("current")
        if value is None:
            return "数据不足"
        return "油价偏高，通胀/地缘扰动" if value > 90 else "油价温和"
    if item == "csi":
        joined = "".join(labels)
        if "A股偏强" in joined:
            return "A股偏强，不追高"
        if "A股反弹" in joined:
            return "A股反弹，等确认"
        if "A股回调" in joined:
            return "A股回调，放慢"
        return "A股偏弱/震荡，放慢"
    if item == "style":
        if "红利占优" in "".join(labels):
            return "红利/低估值相对占优"
        if "成长占优" in "".join(labels):
            return "成长风格占优，但只做业绩共振"
        return "风格不明显"
    return "—"


def build_macro_section(macro: dict | None) -> str:
    if not macro:
        return "## 二、宏观与外部环境\n\n_宏观数据暂不可用，按中性处理。_\n"
    china = macro.get("china") or {}
    glob = macro.get("global") or {}
    regime = classify_regime(macro)
    labels = "、".join(regime.get("labels") or []) or "中性/数据不足"
    indices = {
        item.get("name"): item
        for item in macro.get("indices") or []
        if item.get("name")
    }
    csi = (indices.get("沪深300") or {}).get("tech") or {}
    dividend = (indices.get("红利指数") or {}).get("tech") or {}
    growth = (indices.get("创业板指") or {}).get("tech") or {}

    lines = ["## 二、宏观与外部环境", "", f"**状态**：{labels}。", ""]
    lines.extend(
        [
            "| 指标 | 最新 | 5日变化 | 对股票读法 |",
            "|---|---:|---:|---|",
        ]
    )
    cn10 = china.get("10y")
    cn10_chg = china.get("10y_5d_bp")
    cn10_text = "—" if cn10 is None else f"{cn10:.2f}%"
    lines.append(
        f"| 中国10Y国债 | {cn10_text} | "
        f"{f'{cn10_chg:+.0f}bp' if cn10_chg is not None else '—'} | {_macro_read('cn10', macro)} |"
    )
    us10 = glob.get("us_10y")
    us10_chg = glob.get("us_10y_5d_bp")
    us10_text = "—" if us10 is None else f"{us10:.2f}%"
    lines.append(
        f"| 美国10Y国债 | {us10_text} | "
        f"{f'{us10_chg:+.0f}bp' if us10_chg is not None else '—'} | {_macro_read('us10', macro)} |"
    )
    usdcny = (glob.get("usdcny") or {}).get("current")
    lines.append(
        f"| 美元/人民币 | {fmt_num(usdcny, 3)} | — | {_macro_read('usdcny', macro)} |"
    )
    oil = (glob.get("oil") or {}).get("current")
    lines.append(
        f"| 原油 | {fmt_num(oil, 2)} | — | {_macro_read('oil', macro)} |"
    )
    lines.append(
        f"| 沪深300趋势 | {csi.get('trend') or '—'} | "
        f"{fmt_pct(csi.get('ret_20d'))}（20日） | {_macro_read('csi', macro)} |"
    )
    d20 = dividend.get("ret_20d")
    g20 = growth.get("ret_20d")
    if d20 is None or g20 is None:
        style = "—"
    else:
        style = "红利" if d20 > g20 else "成长"
    lines.append(
        f"| 红利/成长 | {style} | 红利 {fmt_pct(d20)} / 成长 {fmt_pct(g20)}（20日） | "
        f"{_macro_read('style', macro)} |"
    )
    lines.extend(["", "### 对股票的含义", ""])
    impacts = [
        str(item)
        for item in (regime.get("impacts") or [])
        if not any(
            word in str(item)
            for word in ("短债", "纯债", "长债", "久期", "信用债", "二级债")
        )
    ]
    if not impacts:
        impacts = ["宏观信号不极端，按个股质量和价格区间执行，不因单日新闻追涨杀跌。"]
    for item in impacts:
        lines.append(f"- {item}")
    lines.append(
        "- 高估值成长对海外利率和油价更敏感；PE>40 的个股只在回调、趋势企稳时分批，不追单日大涨。"
    )
    news = macro.get("news") or []
    if news:
        lines.extend(["", "### 相关新闻", ""])
        for item in news[:5]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            source = str(item.get("source") or "")
            published = str(item.get("published") or "")
            tags = "、".join(item.get("tags") or [])
            suffix = f"（{source} {published}）" if source or published else ""
            tag_text = f" · {tags}" if tags else ""
            lines.append(f"- **{title}**{suffix}{tag_text}")
    errors = macro.get("errors") or []
    if errors:
        lines.extend(["", f"> 数据提示：{'；'.join(str(item) for item in errors[:3])}。"])
    lines.append("")
    return "\n".join(lines)


def _stock_table(rows: list[dict]) -> str:
    lines = [
        "| 股票 | 代码 | 行业 | 价格 | 当日 | 20日 | 1年 | 评分 | 证据 | PE | 市值(亿) | 趋势 | 风险 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|---|",
    ]
    for row in rows:
        ev = row.get("evidence") or {}
        pe = row.get("pe")
        pe_text = "亏损" if pe is not None and pe < 0 else fmt_num(pe, 1)
        ev_text = (
            f"{fmt_num(ev.get('score'), 1)}·{ev.get('grade', '—')}"
            if ev.get("score") is not None
            else "—"
        )
        lines.append(
            f"| {_cell(row.get('name'))} | {_cell(row.get('code'))} | "
            f"{_cell(row.get('category') or row.get('industry') or '—')} | {fmt_num(row.get('price'))} | "
            f"{fmt_pct(row.get('daily_pct'))} | {fmt_pct(row.get('ret_20d'))} | "
            f"{fmt_pct(row.get('ret_250d'))} | {fmt_num(row.get('score'), 1)} | "
            f"{ev_text} | {pe_text} | {fmt_num(row.get('market_cap_yi'), 1)} | "
            f"{_cell(row.get('trend_label') or '—')} | {_cell(row.get('risk_label') or '—')} |"
        )
    return "\n".join(lines) + "\n"


def _evidence_table(rows: list[dict]) -> str:
    lines = [
        "| 股票 | 代码 | 质量55% | 趋势15% | 位置15% | 宏观10% | 风险5% | 证据分 | 等级 | 读法 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        ev = row.get("evidence") or {}
        grade = ev.get("grade", "—")
        read = {
            "A": "多因子一致，但仍可能亏损",
            "B": "中上，等回调/分批",
            "C": "一般，不加重",
            "D": "弱，先复核",
        }.get(grade, "—")
        lines.append(
            f"| {_cell(row.get('name'))} | {_cell(row.get('code'))} | "
            f"{fmt_num(ev.get('quality'), 1)} | {fmt_num(ev.get('trend'), 1)} | "
            f"{fmt_num(ev.get('position'), 1)} | {fmt_num(ev.get('macro'), 1)} | "
            f"{fmt_num(ev.get('risk'), 1)} | {fmt_num(ev.get('score'), 1)} | "
            f"{grade} | {read} |"
        )
    return "\n".join(lines) + "\n"


def _principles_card(rows: list[dict], limit: int = 8) -> str:
    """A compact checklist for 行动 / 坚持 / 眼光, not a new score.

    The card deliberately repeats the mechanical action and adds the two
    questions that a score cannot answer: what would prove the thesis wrong,
    and whether the valuation/quality is understandable.
    """
    lines = [
        "## 三原则卡（行动 / 坚持 / 眼光）",
        "",
        "> 这不是新评分，也不改变机械动作；三题答不上来就不做。",
        "> 行动=马斯克式先做最小动作；坚持=黄仁勋式长期迭代；眼光=巴菲特式能力圈与安全边际。",
        "",
        "| 标的 | 行动：做什么 | 坚持：逻辑 / 证伪 | 眼光：质量 / 估值 / 位置 |",
        "|---|---|---|---|",
    ]
    for row in rows[:limit]:
        action = row.get("review_action") or "观察"
        buy_range = row.get("buy_range") or "—"
        thesis = (
            row.get("note")
            or row.get("role")
            or row.get("review_reason")
            or "—"
        )
        invalidation = "评分<55 / 证据<55 / 重大公告"
        buy_low, _ = _buy_zone(row)
        if buy_low is not None:
            invalidation += f" / 收盘跌破 {buy_low * 0.92:.2f}"
        ev = row.get("evidence") or {}
        ev_text = fmt_num(ev.get("score"), 1) if isinstance(ev, dict) else "—"
        eye = (
            f"质量{fmt_num(row.get('quality_score'), 1)}、"
            f"PE{fmt_num(row.get('pe'), 1)}、"
            f"位置{fmt_num(row.get('position_score'), 1)}、"
            f"趋势{fmt_num(row.get('trend_score'), 1)}、"
            f"证据{ev_text}"
        )
        lines.append(
            f"| {_cell(row.get('name'))}（{_cell(row.get('code'))}） | "
            f"{_cell(action)}；{_cell(buy_range)} | "
            f"{_cell(thesis)}；证伪：{_cell(invalidation)} | "
            f"{_cell(eye)} |"
        )
    if len(rows) > limit:
        lines.extend(["", f"> 仅展示前 {limit} 只；完整名单见上面的推荐表。"])
    lines.append("")
    return "\n".join(lines)


def _recommendation_table(rows: list[dict], period_label: str) -> str:
    lines = [
        f"| 动作 | 股票 | 代码 | 目标权重 | 实际权重 | 价格 | {period_label} | 评分 | 证据 | 参考买入区间 | 参考卖出区间 | 理由 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        ev = row.get("evidence") or {}
        ev_text = (
            f"{fmt_num(ev.get('score'), 1)}·{ev.get('grade', '—')}"
            if ev.get("score") is not None
            else "—"
        )
        actual = row.get("actual_weight")
        actual_text = "未录入" if actual is None else _weight_text(actual)
        lines.append(
            f"| {row.get('review_action')} | {_cell(row.get('name'))} | {_cell(row.get('code'))} | "
            f"{_weight_text(row.get('target_weight'))} | {actual_text} | "
            f"{fmt_num(row.get('price'))} | {fmt_pct(_period_return(row))} | "
            f"{fmt_num(row.get('score'), 1)} | {ev_text} | "
            f"{_cell(row.get('buy_range'))} | {_cell(row.get('sell_range'))} | "
            f"{_cell(row.get('review_reason'))} |"
        )
    return "\n".join(lines) + "\n"


def _holdings_table(rows: list[dict], holdings: dict | None) -> str:
    holdings = holdings or {}
    if not holdings.get("loaded"):
        return (
            "- 尚未录入实际持仓。把 `stock-data/holdings.json` 填好后，"
            "才会出现真正的“继续持有/减仓/出货”动作。\n"
            "- 当前四组结论按“目标权重/观察池”判断，未持有也会显示“可以跟进持有”，"
            "但不会假装已有仓位。\n"
        )
    held = [row for row in rows if (row.get("current_value") or 0) > 0]
    lines = [
        f"- 实际总资产：**{fmt_money(holdings.get('total_value'))}**；"
        f"现金：**{fmt_money(holdings.get('cash'))}**（{_weight_text(holdings.get('cash_weight'))}）。",
    ]
    if holdings.get("as_of"):
        lines.append(f"- 持仓日期：{holdings.get('as_of')}。")
    unmatched = holdings.get("unmatched") or []
    if unmatched:
        lines.append(
            f"- 持仓文件中有 {len(unmatched)} 条未在观察池：{'、'.join(str(code) for code in unmatched)}；"
            "只按文件里填写的 current_value 计入总资产，未参与个股动作。"
        )
    if not held:
        lines.append("- 持仓文件已加载，但没有匹配到观察池里的股票。")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "",
            "| 股票 | 代码 | 股数 | 成本价 | 现价 | 市值 | 盈亏 | 盈亏% | 实际权重 | 目标权重 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in held:
        lines.append(
            f"| {_cell(row.get('name'))} | {_cell(row.get('code'))} | "
            f"{fmt_num(row.get('shares'), 0)} | {fmt_num(row.get('cost_price'))} | "
            f"{fmt_num(row.get('price'))} | {fmt_money(row.get('current_value'))} | "
            f"{fmt_money(row.get('pnl'))} | {fmt_pct(row.get('pnl_pct'))} | "
            f"{_weight_text(row.get('actual_weight'))} | {_weight_text(row.get('target_weight'))} |"
        )
    return "\n".join(lines) + "\n"


def _announcement_rank(item: dict) -> tuple[int, str]:
    title = str(item.get("title") or "")
    important = (
        "年报", "半年报", "季报", "业绩预告", "业绩快报", "分红", "回购",
        "增持", "减持", "质押", "立案", "处罚", "退市", "重大合同",
        "定增", "收购", "重组", "高管",
    )
    return (0 if any(word in title for word in important) else 1, str(item.get("date") or ""))


def _is_important_announcement(item: dict) -> bool:
    title = str(item.get("title") or "")
    important = (
        "年报", "半年报", "季报", "业绩预告", "业绩快报", "分红", "回购",
        "增持", "减持", "质押", "立案", "处罚", "退市", "重大合同",
        "定增", "收购", "重组", "高管",
    )
    return any(word in title for word in important)


def _announcements_table(rows: list[dict], limit: int = 12) -> str:
    items: list[dict] = []
    for row in rows:
        for ann in row.get("announcements") or []:
            items.append(
                {
                    **ann,
                    "stock_name": row.get("name"),
                    "code": row.get("code"),
                }
            )
    if not items:
        return "_本期没有抓到公告。_\n"
    items.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    items.sort(key=lambda item: 0 if _is_important_announcement(item) else 1)
    lines = ["| 股票 | 日期 | 公告 |", "|---|---|---|"]
    for item in items[:limit]:
        title = _cell(item.get("title") or "")
        url = str(item.get("url") or "")
        title_md = f"[{title}]({url})" if url else title
        lines.append(
            f"| {_cell(item.get('stock_name'))}（{_cell(item.get('code'))}） | "
            f"{_cell(item.get('date'))} | {title_md} |"
        )
    return "\n".join(lines) + "\n"


def _alerts_block(alerts: list[dict], heading: str = "### 今日预警") -> str:
    lines = [heading, ""]
    if not alerts:
        lines.append("- 本期无预警。")
        lines.append("")
        return "\n".join(lines)
    for level in ("high", "warn", "info"):
        items = [alert for alert in alerts if alert.get("level") == level]
        if not items:
            continue
        lines.append(f"**{LEVEL_LABEL.get(level, level)}优先级**")
        for alert in items:
            lines.append(
                f"- **{_cell(alert.get('name'))}（{_cell(alert.get('code'))}）**："
                f"{_cell(alert.get('message'))}"
            )
        lines.append("")
    return "\n".join(lines)


def _weighted_return(
    rows: list[dict],
    key: str,
    holdings: dict | None = None,
) -> tuple[float | None, str]:
    holdings = holdings or {}
    actual_mode = bool(
        holdings.get("loaded")
        and any((row.get("actual_weight") or 0) > 0 for row in rows)
    )
    pairs: list[tuple[float, float]] = []
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        weight = row.get("actual_weight") if actual_mode else row.get("target_weight")
        if weight is None or float(weight) <= 0:
            continue
        pairs.append((float(value), float(weight)))
    if pairs:
        total = sum(weight for _, weight in pairs)
        if total > 0:
            return sum(value * weight for value, weight in pairs) / total, (
                "实际权重" if actual_mode else "目标权重"
            )
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    if values:
        return sum(values) / len(values), "等权观察池"
    return None, "无数据"


def _best_worst(rows: list[dict], key: str) -> tuple[dict | None, dict | None]:
    candidates = [row for row in rows if row.get(key) is not None]
    if not candidates:
        return None, None
    best = max(
        candidates,
        key=lambda row: row[key] if row.get(key) is not None else -999,
    )
    worst = min(
        candidates,
        key=lambda row: row[key] if row.get(key) is not None else 999,
    )
    return best, worst


def _avg_score(rows: list[dict]) -> float | None:
    values = [float(row["score"]) for row in rows if row.get("score") is not None]
    return sum(values) / len(values) if values else None


def _counts_line(rows: list[dict]) -> str:
    counts = _group_counts(rows)
    return (
        f"继续持有 **{counts['继续持有']}** 只，"
        f"可以跟进持有 **{counts['可以跟进持有']}** 只，"
        f"可以调整 **{counts['可以调整']}** 只，"
        f"可以出货 **{counts['可以出货']}** 只，"
        f"暂不动作 **{counts['暂不动作']}** 只"
    )


def _overview_lines(
    rows: list[dict],
    alerts: list[dict],
    holdings: dict | None,
    key: str,
    period_label: str,
) -> list[str]:
    high = [alert for alert in alerts if alert.get("level") == "high"]
    weighted, basis = _weighted_return(rows, key, holdings)
    best, worst = _best_worst(rows, key)
    avg = _avg_score(rows)
    lines = [
        f"- 观察池：**{len(rows)} 只**；平均评分 **{fmt_num(avg, 1)}**；"
        f"高优先级预警 **{len(high)} 条**。",
        f"- {period_label}加权收益：**{fmt_pct(weighted)}**（按{basis}，未计现金）。",
        f"- {period_label}最强：**{best.get('name') if best else '—'}**"
        f"（{fmt_pct(best.get(key)) if best else '—'}）；"
        f"最弱：**{worst.get('name') if worst else '—'}**"
        f"（{fmt_pct(worst.get(key)) if worst else '—'}）。",
        f"- 四组结论：{_counts_line(rows)}。",
    ]
    if holdings and holdings.get("loaded"):
        lines.append(
            f"- 实际持仓已接入：总资产 {fmt_money(holdings.get('total_value'))}，"
            f"现金 {_weight_text(holdings.get('cash_weight'))}。"
        )
    else:
        lines.append(
            "- 实际持仓未录入；四组按目标权重/观察池判断，"
            "不会把“可以跟进持有”当成已有仓位。"
        )
    return lines


def _data_error_section(errors: list[str]) -> str:
    if not errors:
        return ""
    lines = ["## 数据异常", "", "> 以下股票本次未纳入报告；不要用旧报告代替。", ""]
    lines.extend(f"- {_cell(error)}" for error in errors)
    lines.append("")
    return "\n".join(lines)


def _rules_section(heading: str = "## 九、规则说明") -> str:
    return "\n".join(
        [
            heading,
            "",
            "- **股票不是基金**：单票波动、停牌、涨跌停、公告跳空都更剧烈；"
            "单票计划仓位建议不超过股票资金的 10%–15%，除非你明确愿意承担更大波动。",
            "- **参考买入区间**取近一年价格区间的 20%–40% 分位；"
            "**参考卖出区间**取 80%–100% 分位。它们是机械参考，不是目标价预测。",
            "- **动作是相对目标权重**：已到目标就持有，未建满才分批；"
            "实际持仓录入后，才会出现真正的减仓/出货动作。",
            "- **回撤/波动类“高优先级”是风险提示，不是卖出信号**；"
            "只有重大公告、ST/退市、目标权重为 0 或进入止盈区，才进入“可以出货”。",
            "- **A股 T+1、涨跌停、停牌、公告跳空**都会让参考区间失效；下单前以实时行情为准。",
            "- **证据分不是胜率**：80+ 只代表质量、趋势、位置、宏观、风险五项更一致，"
            "仍可能亏损；没有任何公开模型能保证单只股票 1–3 年上涨概率 >80%。",
            "- 本工具不能查看账户、不能代下单、不能保证收益，也不是持牌投顾；"
            "所有动作分批执行，先控制单票和行业集中度。",
            "",
        ]
    )


def _footer() -> str:
    return (
        "---\n\n"
        f"_生成时间：{date.today().isoformat()} · 本地工具 stock-tracker v0.1.0_\n"
    )


def enrich_rows(
    rows: list[dict],
    alerts: list[dict],
    evidence: dict | None,
) -> list[dict]:
    alerts_by_code: dict[str, list[dict]] = {}
    for alert in alerts:
        alerts_by_code.setdefault(str(alert.get("code") or ""), []).append(alert)
    return _enrich_recommendations(rows, alerts_by_code, evidence)


def generate_daily_report(
    run_date: str,
    rows: list[dict],
    market: list[dict],
    alerts: list[dict],
    config: dict,
    macro_section: str = "",
    evidence: dict | None = None,
    holdings: dict | None = None,
    next_review_date: str | None = None,
    errors: list[str] | None = None,
    learning_section: str = "",
    ai_section: str = "",
) -> str:
    enriched = enrich_rows(rows, alerts, evidence)
    next_date = next_review_date or next_trading_date(run_date, 2)
    price_dates = [str(row.get("price_date")) for row in rows if row.get("price_date")]
    as_of = max(price_dates) if price_dates else "—"
    lines = [
        f"# 股票追踪日报 · {run_date}",
        "",
        "> 模块：股票追踪（独立于基金）。",
        f"> 观察池 {len(rows)} 只；行情数据截至 {as_of}。",
        "> 本报告按机械规则生成，参考区间不是预测，不构成投资建议，也不保证收益。",
        "",
        "## 一、市场快照",
        "",
        _market_table(market),
    ]
    if macro_section:
        lines.append(macro_section)
    else:
        lines.append("## 二、宏观与外部环境\n\n_宏观数据暂未启用。_\n")
    if ai_section:
        lines.extend([ai_section, ""])
    lines.extend(
        [
            "## 三、观察池概览",
            "",
            *_overview_lines(enriched, alerts, holdings, "daily_pct", "当日"),
            "",
            "## 四、个股明细",
            "",
            _stock_table(enriched),
            _group_section(enriched, heading="## 五、四组预览"),
            "## 六、实际持仓与公告",
            "",
            "### 实际持仓",
            "",
            _holdings_table(enriched, holdings),
            _alerts_block(alerts, heading="### 今日预警"),
            "### 重要公告",
            "",
            _announcements_table(enriched),
            _data_error_section(errors or []),
            *([learning_section, ""] if learning_section else []),
            _rules_section("## 七、规则说明"),
            "## 八、下一次 2 日推荐",
            "",
            f"- 预计 **{next_date}** 左右（2 个交易日后）；周五另出周度总结，月末另出月度总结。",
            "- 日报每天生成；四组动作以 2 日推荐和实际持仓为准。",
            "",
            _footer(),
        ]
    )
    return "\n".join(lines)


def generate_review(
    review_date: str,
    period_start: str,
    period_end: str,
    rows: list[dict],
    market: list[dict],
    alerts: list[dict],
    config: dict,
    next_review_date: str | None = None,
    macro_section: str = "",
    evidence: dict | None = None,
    window_days: int = 2,
    holdings: dict | None = None,
    learning_section: str = "",
    ai_section: str = "",
) -> str:
    window_days = max(1, int(window_days))
    enriched = enrich_rows(rows, alerts, evidence)
    next_date = next_review_date or next_trading_date(review_date, window_days)
    weighted, basis = _weighted_return(enriched, "ret_window", holdings)
    best, worst = _best_worst(enriched, "ret_window")
    csi = next((item for item in market if item.get("name") == "沪深300"), None)
    csi_window = None
    if csi:
        csi_window = csi.get("pct_window")
        if csi_window is None:
            csi_window = csi.get("pct_5d")

    lines = [
        f"# 股票追踪 · {window_days} 日推荐 · {review_date}",
        "",
        "> 模块：股票追踪（独立于基金）。",
        f"> 覆盖区间：{period_start} → {period_end}（约 {window_days} 个交易日）。",
        f"> 行情数据截至 {max([str(r.get('price_date')) for r in rows if r.get('price_date')] or ['—'])}；"
        "若报告日无新行情，动作按最近交易日顺延。",
        "> 本报告按机械规则生成，参考区间不是预测，不构成投资建议，也不保证收益。",
        "",
        f"## 一、{window_days} 日市场",
        "",
        _period_market_table(market, f"{window_days} 日"),
    ]
    if macro_section:
        lines.append(macro_section)
    else:
        lines.append("## 二、宏观与外部环境\n\n_宏观数据暂未启用。_\n")
    if ai_section:
        lines.extend([ai_section, ""])
    lines.extend(
        [
            f"## 三、观察池 {window_days} 日表现",
            "",
            f"- 组合/观察池 {window_days} 日加权收益：**{fmt_pct(weighted)}**（按{basis}，未计现金）。",
            f"- {window_days} 日最强：**{best.get('name') if best else '—'}**"
            f"（{fmt_pct(best.get('ret_window')) if best else '—'}）；"
            f"最弱：**{worst.get('name') if worst else '—'}**"
            f"（{fmt_pct(worst.get('ret_window')) if worst else '—'}）。",
            f"- 四组结论：{_counts_line(enriched)}。",
        ]
    )
    if weighted is not None and csi_window is not None:
        lines.append(
            f"- 相对沪深300：**{weighted - csi_window:+.2f} 个百分点**"
            f"（组合 {weighted:+.2f}% vs 沪深300 {csi_window:+.2f}%）。"
        )
    if holdings and holdings.get("loaded"):
        lines.append(
            f"- 实际持仓已接入：总资产 {fmt_money(holdings.get('total_value'))}，"
            f"现金 {_weight_text(holdings.get('cash_weight'))}。"
        )
    else:
        lines.append(
            "- 实际持仓未录入；本期四组按目标权重/观察池判断，"
            "真正的“继续持有/减仓”要等持仓文件接入。"
        )
    lines.extend(
        [
            "",
            _group_section(enriched, heading="## 四、四组结论"),
            "## 五、证据面板",
            "",
            _evidence_table(enriched),
            "## 六、推荐名单与参考区间",
            "",
            _recommendation_table(enriched, f"{window_days} 日"),
            _principles_card(enriched),
            _group_checklist(enriched, heading="## 七、执行清单"),
            "## 八、实际持仓与公告",
            "",
            "### 实际持仓",
            "",
            _holdings_table(enriched, holdings),
            _alerts_block(alerts, heading="### 本期预警"),
            "### 重要公告",
            "",
            _announcements_table(enriched, limit=16),
            *([learning_section, ""] if learning_section else []),
            _rules_section("## 九、规则说明"),
            "## 十、下一次推荐",
            "",
            f"- 预计 **{next_date}** 左右（{window_days} 个交易日后）。",
            "- 每日日报仍会自动生成；每满 2 个交易日才出这份推荐。",
            "- 周度总结在周五，月度总结在每月最后一个工作日；若与 2 日推荐同日，默认都生成。",
            "",
            _footer(),
        ]
    )
    return "\n".join(lines)


def generate_summary_report(
    kind: str,
    report_date: str,
    period_start: str,
    period_end: str,
    rows: list[dict],
    market: list[dict],
    alerts: list[dict],
    config: dict,
    next_report_date: str | None = None,
    macro_section: str = "",
    evidence: dict | None = None,
    holdings: dict | None = None,
    learning_section: str = "",
    ai_section: str = "",
) -> str:
    is_weekly = kind == "weekly"
    period_label = "本周" if is_weekly else "本月"
    title = "股票追踪 · 周度总结" if is_weekly else "股票追踪 · 月度总结"
    enriched = enrich_rows(rows, alerts, evidence)
    weighted, basis = _weighted_return(enriched, "ret_window", holdings)
    best, worst = _best_worst(enriched, "ret_window")
    csi = next((item for item in market if item.get("name") == "沪深300"), None)
    csi_window = None
    if csi:
        csi_window = csi.get("pct_window")
        if csi_window is None:
            csi_window = csi.get("pct_5d")
    next_date = next_report_date
    if not next_date:
        next_date = next_trading_date(report_date, 5) if is_weekly else next_month_last_trading_day(report_date)

    lines = [
        f"# {title} · {report_date}",
        "",
        "> 模块：股票追踪（独立于基金）。",
        f"> 覆盖区间：{period_start} → {period_end}（{period_label}；按交易日计算，遇长假顺延）。",
        f"> 行情数据截至 {max([str(r.get('price_date')) for r in rows if r.get('price_date')] or ['—'])}；"
        "若报告日无新行情，动作按最近交易日顺延。",
        "> 本报告按机械规则生成，参考区间不是预测，不构成投资建议，也不保证收益。",
        "",
        f"## 一、{period_label}市场",
        "",
        _period_market_table(market, period_label),
    ]
    if macro_section:
        lines.append(macro_section)
    else:
        lines.append("## 二、宏观与外部环境\n\n_宏观数据暂未启用。_\n")
    if ai_section:
        lines.extend([ai_section, ""])
    lines.extend(
        [
            f"## 三、观察池{period_label}表现",
            "",
            f"- 组合/观察池{period_label}加权收益：**{fmt_pct(weighted)}**（按{basis}，未计现金）。",
            f"- {period_label}最强：**{best.get('name') if best else '—'}**"
            f"（{fmt_pct(best.get('ret_window')) if best else '—'}）；"
            f"最弱：**{worst.get('name') if worst else '—'}**"
            f"（{fmt_pct(worst.get('ret_window')) if worst else '—'}）。",
            f"- 四组结论：{_counts_line(enriched)}。",
        ]
    )
    if weighted is not None and csi_window is not None:
        lines.append(
            f"- 相对沪深300：**{weighted - csi_window:+.2f} 个百分点**"
            f"（组合 {weighted:+.2f}% vs 沪深300 {csi_window:+.2f}%）。"
        )
    if holdings and holdings.get("loaded"):
        lines.append(
            f"- 实际持仓已接入：总资产 {fmt_money(holdings.get('total_value'))}，"
            f"现金 {_weight_text(holdings.get('cash_weight'))}。"
        )
    else:
        lines.append(
            "- 实际持仓未录入；本期四组按目标权重/观察池判断。"
        )
    lines.extend(
        [
            "",
            _group_section(enriched, heading="## 四、四组结论"),
            "## 五、证据面板",
            "",
            _evidence_table(enriched),
            "## 六、推荐名单与参考区间",
            "",
            _recommendation_table(enriched, period_label),
            _principles_card(enriched),
            _group_checklist(enriched, heading="## 七、执行清单"),
            "## 八、实际持仓与公告",
            "",
            "### 实际持仓",
            "",
            _holdings_table(enriched, holdings),
            _alerts_block(alerts, heading=f"### {period_label}预警"),
            "### 重要公告",
            "",
            _announcements_table(enriched, limit=20),
            *([learning_section, ""] if learning_section else []),
            _rules_section("## 九、规则说明"),
            f"## 十、下一次{'周度' if is_weekly else '月度'}总结",
            "",
            f"- 预计 **{next_date}** 左右。",
            "- 周度总结在周五；月度总结在每月最后一个工作日。",
            "",
            _footer(),
        ]
    )
    return "\n".join(lines)


def summary_text_daily(
    run_date: str,
    rows: list[dict],
    alerts: list[dict],
    report_path: str = "",
) -> str:
    high = [alert for alert in alerts if alert.get("level") == "high"]
    text = (
        f"{run_date}：{len(rows)}只，平均{fmt_num(_avg_score(rows), 1)}分，"
        f"高优先级预警{len(high)}条；四组：{_counts_line(rows)}。"
    )
    if report_path:
        text += f"报告：{report_path}"
    return text


def summary_text_review(
    review_date: str,
    rows: list[dict],
    alerts: list[dict],
    report_path: str = "",
) -> str:
    high = [alert for alert in alerts if alert.get("level") == "high"]
    text = (
        f"{review_date}：{len(rows)}只，2日推荐，平均{fmt_num(_avg_score(rows), 1)}分，"
        f"高优先级预警{len(high)}条；四组：{_counts_line(rows)}。"
    )
    if report_path:
        text += f"报告：{report_path}"
    return text


def summary_text_summary(
    report_date: str,
    kind: str,
    rows: list[dict],
    alerts: list[dict],
    report_path: str = "",
) -> str:
    label = "周度" if kind == "weekly" else "月度"
    high = [alert for alert in alerts if alert.get("level") == "high"]
    text = (
        f"{report_date}：{len(rows)}只，{label}总结，平均{fmt_num(_avg_score(rows), 1)}分，"
        f"高优先级预警{len(high)}条；四组：{_counts_line(rows)}。"
    )
    if report_path:
        text += f"报告：{report_path}"
    return text
