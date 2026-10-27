from __future__ import annotations

from collections import Counter
from typing import Any

from .macro import classify_regime

EQUITY_CATEGORIES = {"红利", "宽基", "权益", "转债", "二级债/固收+"}
BOND_CATEGORIES = {"短债", "纯债"}

ANNOUNCEMENT_RISK_HIGH = (
    "基金经理变更",
    "暂停申购",
    "清盘",
    "清算",
    "风险提示",
    "终止",
)
ANNOUNCEMENT_RISK_WARN = (
    "大额申购",
    "限购",
    "恢复申购",
    "分红",
)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _tech_trend_score(row: dict) -> tuple[float, str]:
    tech = row.get("tech") or {}
    if not tech:
        return 50.0, "技术面数据不足"
    score = 50.0
    above20 = tech.get("above_ma20")
    above60 = tech.get("above_ma60")
    ma20 = tech.get("ma20")
    ma60 = tech.get("ma60")
    rsi = tech.get("rsi14")
    ret20 = tech.get("ret_20d")
    if above20 is True:
        score += 15
    elif above20 is False:
        score -= 15
    if above60 is True:
        score += 15
    elif above60 is False:
        score -= 15
    if ma20 is not None and ma60 is not None:
        score += 10 if ma20 > ma60 else -10
    if rsi is not None:
        if 40 <= rsi <= 65:
            score += 10
        elif rsi > 75:
            score -= 10
        elif rsi < 30:
            score += 5
    if ret20 is not None:
        score += 10 if ret20 > 0 else -5
    trend = tech.get("trend") or "数据不足"
    return _clamp(score), f"K线趋势{trend}，RSI {rsi if rsi is not None else '—'}"


def _bond_trend_score(row: dict) -> tuple[float, str]:
    score = 70.0
    dd1 = row.get("dd_1y")
    from_high = row.get("from_high_1y")
    ret1m = row.get("ret_1m")
    if dd1 is not None:
        if abs(dd1) <= 0.5:
            score += 15
        elif abs(dd1) <= 1.5:
            score += 5
        elif abs(dd1) > 3:
            score -= 20
    if from_high is not None:
        if from_high > -0.3:
            score += 10
        elif from_high < -1.5:
            score -= 10
    if ret1m is not None:
        score += 5 if ret1m > 0 else -5
    return _clamp(score), f"近1年回撤{dd1 if dd1 is not None else '—'}%，距高点{from_high if from_high is not None else '—'}%"


def _position_score(row: dict, category: str) -> tuple[float, str]:
    range_pct = row.get("range_pct_1y")
    from_high = row.get("from_high_1y")
    if category in EQUITY_CATEGORIES and range_pct is not None:
        score = 100 - float(range_pct)
        return _clamp(score), f"近1年净值百分位{range_pct:.0f}%（越低越不追高）"
    if from_high is not None:
        score = 100 - min(50.0, abs(float(from_high)) * 15)
        return _clamp(score), f"净值位置分 {score:.0f}/100"
    return 50.0, "位置数据不足"


def _macro_score(row: dict, macro: dict, news_tags: Counter) -> tuple[float, str]:
    category = row.get("category") or "其他"
    regime = classify_regime(macro)
    labels = set(regime.get("labels") or [])
    score = 60.0
    reasons: list[str] = []

    if category in BOND_CATEGORIES:
        if "国内低利率" in labels:
            score += 10
            reasons.append("国内低利率支撑债底")
        if "国内利率近5日上行" in labels:
            score -= 15
            reasons.append("国内利率近5日上行")
        if "国内利率近5日下行" in labels:
            score += 8
            reasons.append("国内利率近5日下行")
        if "油价偏高" in labels:
            score -= 5
            reasons.append("油价偏高带来通胀扰动")
        if news_tags.get("信用/财政", 0) >= 2:
            score -= 8
            reasons.append("信用/财政新闻偏多")
    elif category == "二级债/固收+":
        if "国内低利率" in labels:
            score += 8
            reasons.append("国内低利率支撑债底")
        if "A股偏强" in labels:
            score += 10
            reasons.append("A股趋势偏强")
        elif "A股偏弱" in labels:
            score -= 10
            reasons.append("A股趋势偏弱")
        if "海外高利率" in labels:
            score -= 5
            reasons.append("海外高利率压制估值")
        if "油价偏高" in labels:
            score -= 5
            reasons.append("油价偏高")
    elif category == "红利":
        if "红利占优" in labels:
            score += 15
            reasons.append("红利风格占优")
        if "成长占优" in labels:
            score -= 10
            reasons.append("成长风格占优")
        if "海外高利率" in labels:
            score += 5
            reasons.append("高利率环境利于价值/红利相对防御")
        if "A股偏弱" in labels:
            score += 3
            reasons.append("弱市中红利相对防御")
    else:
        if "A股偏强" in labels:
            score += 15
            reasons.append("A股趋势偏强")
        elif "A股偏弱" in labels:
            score -= 15
            reasons.append("A股趋势偏弱")
        if "海外高利率" in labels:
            score -= 5
            reasons.append("海外高利率压制成长估值")
        if "人民币偏弱" in labels:
            score -= 5
            reasons.append("人民币偏弱")
        if "成长占优" in labels:
            score += 5
            reasons.append("成长风格占优")
        if "红利占优" in labels:
            score -= 3
            reasons.append("红利风格占优")

    if not reasons:
        reasons.append("宏观信号中性")
    return _clamp(score), "；".join(reasons[:3])


def _risk_score(row: dict, alerts: list[dict], news_tags: Counter) -> tuple[float, str]:
    score = 100.0
    reasons: list[str] = []
    high = sum(1 for alert in alerts if alert.get("level") == "high")
    warn = sum(1 for alert in alerts if alert.get("level") == "warn")
    if high:
        score -= 20 * high
        reasons.append(f"{high}条高优先级预警")
    if warn:
        score -= 8 * warn
        reasons.append(f"{warn}条中优先级预警")
    if row.get("stale_days", 0) > 5:
        score -= 20
        reasons.append("净值未更新")
    holdings = row.get("holdings") or {}
    category = row.get("category") or ""
    if category in EQUITY_CATEGORIES:
        top_stock = holdings.get("top_stock_weight") or 0
        if top_stock > 60:
            score -= 20
            reasons.append("股票持仓过度集中")
        elif top_stock > 40:
            score -= 10
            reasons.append("股票持仓较集中")
    if category in BOND_CATEGORIES:
        top_bond = holdings.get("top_bond_weight") or 0
        if top_bond > 50:
            score -= 15
            reasons.append("债券持仓较集中")
        elif top_bond > 35:
            score -= 8
            reasons.append("债券持仓偏集中")
    for announcement in (row.get("announcements") or [])[:8]:
        title = str(announcement.get("title") or "")
        if any(keyword in title for keyword in ANNOUNCEMENT_RISK_HIGH):
            score -= 15
            reasons.append("公告风险：" + title[:24])
            break
        if any(keyword in title for keyword in ANNOUNCEMENT_RISK_WARN):
            score -= 5
            reasons.append("公告提示：" + title[:24])
            break
    if news_tags.get("地缘", 0) >= 3 and category in EQUITY_CATEGORIES:
        score -= 5
        reasons.append("地缘新闻偏多")
    if not reasons:
        reasons.append("无明显风险项")
    return _clamp(score), "；".join(reasons[:3])


def build_evidence(
    rows: list[dict],
    macro: dict,
    alerts_by_code: dict[str, list[dict]],
) -> dict[str, dict]:
    news_tags: Counter = Counter()
    for item in (macro or {}).get("news") or []:
        for tag in item.get("tags") or []:
            news_tags[tag] += 1

    result: dict[str, dict] = {}
    for row in rows:
        category = row.get("category") or "其他"
        quality = float(row.get("score") or 0)
        if category in EQUITY_CATEGORIES:
            trend, trend_reason = _tech_trend_score(row)
        elif category in BOND_CATEGORIES:
            trend, trend_reason = _bond_trend_score(row)
        else:
            trend, trend_reason = _tech_trend_score(row)
        position, position_reason = _position_score(row, category)
        macro_score, macro_reason = _macro_score(row, macro, news_tags)
        risk, risk_reason = _risk_score(
            row,
            alerts_by_code.get(row.get("code", ""), []),
            news_tags,
        )
        total = (
            quality * 0.55
            + trend * 0.15
            + position * 0.15
            + macro_score * 0.10
            + risk * 0.05
        )
        total = round(_clamp(total), 1)
        if total >= 80:
            grade, label = "A", "证据强"
        elif total >= 70:
            grade, label = "B", "证据中上"
        elif total >= 60:
            grade, label = "C", "证据一般"
        else:
            grade, label = "D", "证据弱"
        reasons = [
            f"质量分 {quality:.1f}",
            trend_reason,
            position_reason,
            macro_reason,
            risk_reason,
        ]
        result[row.get("code", "")] = {
            "score": total,
            "grade": grade,
            "label": label,
            "parts": {
                "quality": round(quality, 1),
                "trend": round(trend, 1),
                "position": round(position, 1),
                "macro": round(macro_score, 1),
                "risk": round(risk, 1),
            },
            "reasons": reasons,
            "macro_reason": macro_reason,
            "risk_reason": risk_reason,
        }
    return result

def evidence_for(row: dict, evidence: dict | None) -> dict:
    if not evidence:
        return {}
    return (evidence.get("results") or {}).get(str(row.get("code") or ""), {})
