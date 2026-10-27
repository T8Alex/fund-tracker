"""Shared report rule engine — parameterized for both funds and stocks."""
from __future__ import annotations

from .format_utils import ACTION_ORDER, fmt_pct, fmt_num, _weight_text, _period_return, _period_text

# Default severe alert words (fund adds "基金经理变更")
DEFAULT_SEVERE_ALERT_WORDS = (
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


def _severe_alerts(alerts: list[dict], severe_words: tuple[str, ...]) -> tuple[list[dict], str]:
    """Filter severe alerts and return (severe_list, joined_text)."""
    high_alerts = [a for a in alerts if a.get("level") == "high"]
    severe = [
        a for a in high_alerts
        if any(w in str(a.get("message", "")) for w in severe_words)
    ]
    text = "；".join(str(a.get("message", "")) for a in severe)
    return severe, text


def _in_price_zone(row: dict, buy_low: float | None, buy_high: float | None, price_key: str) -> bool:
    """Check if current price/nav is within the buy zone."""
    if buy_low is None or buy_high is None:
        return False
    current = row.get(price_key)
    if current is None:
        return False
    try:
        return buy_low <= float(current) <= buy_high
    except (TypeError, ValueError):
        return False


def _get_current_price(row: dict, price_key: str) -> float | None:
    """Get current price or nav value."""
    val = row.get(price_key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _review_action(
    row: dict,
    alerts: list[dict],
    evidence: dict | None = None,
    *,
    price_key: str = "nav",
    has_actual_path: bool = True,
    has_pullback_probe: bool = True,
    severe_words: tuple[str, ...] = DEFAULT_SEVERE_ALERT_WORDS,
    action_overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Core review action engine, parameterized for funds and stocks.

    Parameters:
        price_key: field name for current price ('nav' for funds, 'price' for stocks)
        has_actual_path: whether to handle real holdings (True=fund, False=stock)
        has_pullback_probe: whether to include pullback probing logic
        severe_words: words that trigger severe alerts
        action_overrides: map internal action names to display names
    """
    target = float(row.get("target_weight") or 0)
    actual = row.get("actual_weight")
    score = row.get("score")
    ev_score = (evidence or {}).get("score")
    period_text = _period_text(row)
    ret_period = _period_return(row)
    severe_alerts, severe_text = _severe_alerts(alerts, severe_words)

    # Resolve action display names
    def _action(name: str) -> str:
        if action_overrides and name in action_overrides:
            return action_overrides[name]
        return name

    # ---- Real holding path (funds only) ----
    if has_actual_path and actual is not None and float(actual) > 0:
        if target <= 0:
            return _action("剔除"), "目标 0%；按纪律清仓/换股，不补仓"
        # Rebalancing: actual significantly above target
        if target > 0 and float(actual) > target * 1.25:
            return (
                _action("再平衡"),
                f"实际权重 {_weight_text(actual)} 高于目标 {_weight_text(target)} 的 1.25 倍；先再平衡到目标",
            )
        if severe_alerts:
            return _action("出货/复核"), f"重大预警：{severe_text}"
        if score is None:
            return _action("复核"), "数据不足，先核对公告和行情"
        # Sell zone check
        sell_low = row.get("_sell_low")
        current_price = _get_current_price(row, price_key)
        if sell_low is not None and current_price is not None and current_price >= sell_low:
            price_display = fmt_num(current_price, 4)
            return (
                _action("止盈/减仓"),
                f"{price_key[:3]}值 {price_display} 已进入近1年高位止盈区；分批减 1/3，不一次清仓",
            )
        if score < 45:
            return (
                _action("出货/复核"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "低于清仓复核线，先减 1/3 并核对公告",
            )
        if score < 55 or (ev_score is not None and ev_score < 55):
            return (
                _action("减仓/复核"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "低于防守线，先减 1/3 或暂停加仓",
            )
        excess = float(actual) - target
        if excess > max(0.02, target * 0.25):
            return (
                _action("减仓/复核"),
                f"实际权重 {_weight_text(actual)} 高于目标 {_weight_text(target)}；先再平衡",
            )
        # Add position
        if (
            target > 0
            and float(actual) < target * 0.70
            and score >= 75
            and (ev_score is None or ev_score >= 70)
        ):
            return (
                _action("分批买入/持有"),
                f"低于目标权重，评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；按区间补",
            )
        if score >= 80 and (ev_score is None or ev_score >= 75):
            return (
                _action("持有"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；维持目标权重",
            )
        if score >= 70 and (ev_score is None or ev_score >= 65):
            return (
                _action("持有"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                "维持目标权重，暂不加重",
            )
        if ev_score is not None and ev_score < 65:
            return (
                _action("复核"),
                f"证据分 {ev_score:.1f} 偏低；先核对公告，必要时减 1/3",
            )
        return _action("观察"), f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；暂不加重"

    # ---- Watchlist / target-weight path ----
    if severe_alerts:
        return _action("减仓/复核" if has_actual_path else "复核"), f"重大预警：{severe_text}"
    if target <= 0:
        return _action("观察"), "不占仓；等评分、证据分、回调和趋势同时确认"
    if score is None:
        return _action("观察"), "数据不足"
    if score < 55 or (ev_score is not None and ev_score < 55):
        return _action("观察"), f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；不建仓"

    # Pullback probe (funds only)
    if has_pullback_probe and ret_period is not None and ret_period <= -3 and score is not None and score >= 85:
        return (
            _action("小仓试探"),
            f"{period_text}回撤 {fmt_pct(ret_period)}，证据分 {ev_score if ev_score is not None else '—'}；试探建仓",
        )
    if has_pullback_probe and score is not None and score >= 85 and (ev_score is None or ev_score >= 75):
        if ret_period is None or ret_period > 0:
            return _action("观察"), "没有回调，等净值回落再考虑"

    buy_low = row.get("_buy_low")
    buy_high = row.get("_buy_high")
    in_zone = _in_price_zone(row, buy_low, buy_high, price_key)
    has_position = actual is not None and float(actual) > 0

    if score >= 80 and (ev_score is None or ev_score >= 75):
        if in_zone:
            return (
                _action("分批买入/持有"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                f"{'净值' if price_key == 'nav' else '价格'}在参考区间，分 2–3 批",
            )
        if not has_position:
            return (
                _action("分批买入/持有"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；未持有，按目标建仓",
            )
        if has_pullback_probe and ret_period is not None and ret_period <= -3:
            return (
                _action("小仓试探"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；"
                f"{period_text}回撤 {fmt_pct(ret_period)}，等企稳后分批",
            )
        return _action("观察"), f"证据强，但{'净值' if price_key == 'nav' else '价格'}不在参考买入区间；不追高，等回调"

    if score > 70:
        if ev_score is not None and ev_score >= 75:
            if in_zone:
                return (
                    _action("分批买入/持有"),
                    f"评分 {score:.1f}、证据分 {ev_score}；{'净值' if price_key == 'nav' else '价格'}在参考区间，分 2–3 批",
                )
            return _action("观察"), "评分中上，但不在参考买入区间；等回调"
        if ev_score is None:
            return (
                _action("持有"),
                f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；维持目标权重，暂不加重",
            )
        if ev_score <= 55:
            return (
                _action("减仓/复核"),
                f"评分 {score:.1f}、证据分 {ev_score}；证据偏弱，先减 1/3 或暂停加仓",
            )
        # ev_score in (55, 70): mid evidence, hold but don't add weight
        return (
            _action("持有"),
            f"评分 {score:.1f}、证据分 {ev_score}；证据中等，维持目标权重，暂不加重",
        )

    if has_pullback_probe and ret_period is not None and ret_period <= -8 and score >= 60 and (ev_score or 0) >= 60:
        return _action("观察"), f"{period_text}回撤 {fmt_pct(ret_period)}；只观察，不接飞刀"
    return _action("观察"), f"评分 {score:.1f}、证据分 {ev_score if ev_score is not None else '—'}；暂不加重"


def _recommendation_bucket(row: dict, action: str, evidence: dict | None, *, allow_adjust_for_observing: bool = True) -> str:
    """
    Map an action to a recommendation group.

    Parameters:
        allow_adjust_for_observing: if True, '观察' with target>0 maps to '可以调整'
    """
    target = float(row.get("target_weight") or 0)
    actual = row.get("actual_weight")
    ev_score = (evidence or {}).get("score")

    if action in {"止盈/减仓", "出货/复核"}:
        if actual is not None and float(actual) > 0:
            return "可以出货"
        return "暂不动作"
    if action in {"减仓/复核", "剔除", "再平衡"}:
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
        if allow_adjust_for_observing and target > 0:
            return "可以调整"
        return "暂不动作"
    if action in {"小仓试探", "分批买入/持有"}:
        return "可以跟进持有"
    if action == "持有":
        if target > 0 and ev_score is not None and ev_score < 70:
            return "可以调整"
        return "继续持有"
    return "暂不动作"


def _enrich_recommendations(
    rows: list[dict],
    alerts_by_code: dict[str, list[dict]],
    evidence: dict | None,
    evidence_for_fn,
    review_action_fn,
    recommendation_bucket_fn,
    buy_range_fn,
    sell_range_fn,
    **action_kwargs,
) -> list[dict]:
    """
    Enrich rows with review actions, evidence, and recommendation groups.

    Parameters:
        evidence_for_fn: function to get evidence for a row (domain-specific)
        review_action_fn: the parameterized _review_action function
        recommendation_bucket_fn: the parameterized _recommendation_bucket function
        buy_range_fn, sell_range_fn: domain-specific range formatters
        **action_kwargs: kwargs passed to _review_action (price_key, etc.)
    """
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        item_evidence = evidence_for_fn(item, evidence)
        action, reason = review_action_fn(
            item,
            alerts_by_code.get(str(item.get("code") or ""), []),
            item_evidence,
            **action_kwargs,
        )
        item["evidence"] = item_evidence
        item["review_action"] = action
        item["review_reason"] = reason
        item["recommendation_group"] = recommendation_bucket_fn(item, action, item_evidence)
        item["buy_range"] = buy_range_fn(item)
        item["sell_range"] = sell_range_fn(item)
        enriched.append(item)

    enriched.sort(
        key=lambda item: (
            ACTION_ORDER.get(item.get("review_action"), 9),
            -(item.get("score") or 0),
        )
    )
    return enriched
