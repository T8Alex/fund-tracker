from __future__ import annotations

import math
from datetime import date
from typing import Any

from fund_tracker.metrics import (
    annualized_return,
    drawdown_from_high,
    max_drawdown,
    nav_range,
    price_technicals,
    sharpe,
    total_return,
    volatility,
)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _linear(value: float | None, low: float, high: float) -> float:
    """Map low→0, high→100, clamped. Missing data is neutral 50."""
    if value is None or high <= low:
        return 50.0
    return _clamp((float(value) - low) / (high - low) * 100.0)


def _reverse(value: float | None, low: float, high: float) -> float:
    if value is None or high <= low:
        return 50.0
    return 100.0 - _linear(value, low, high)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _is_financial(industry: str | None) -> bool:
    text = str(industry or "")
    return any(word in text for word in ("银行", "保险", "证券", "多元金融", "信托"))


def _market_cap_score(cap_yi: float | None) -> float:
    if cap_yi is None:
        return 50.0
    if cap_yi >= 2000:
        return 95.0
    if cap_yi >= 1000:
        return 85.0
    if cap_yi >= 500:
        return 75.0
    if cap_yi >= 200:
        return 65.0
    if cap_yi >= 100:
        return 55.0
    if cap_yi >= 50:
        return 40.0
    return 25.0


def _liquidity_score(turnover: float | None) -> float:
    if turnover is None:
        return 50.0
    if 0.5 <= turnover <= 5.0:
        return 80.0
    if 5.0 < turnover <= 10.0:
        return 60.0
    if turnover > 10.0:
        return 35.0
    return 40.0


def quality_score(
    quote: dict,
    financials: dict,
) -> tuple[float, dict[str, dict[str, float | None]]]:
    roe = financials.get("roe")
    revenue_growth = financials.get("revenue_growth")
    profit_growth = financials.get("profit_growth")
    debt = financials.get("debt_ratio")
    gross = financials.get("gross_margin")
    cash = financials.get("cash_ratio")
    pe = quote.get("pe")
    pb = quote.get("pb")
    market_cap_yi = (
        quote.get("market_cap") / 1e8
        if quote.get("market_cap") is not None
        else None
    )
    turnover = quote.get("turnover")
    industry = quote.get("industry")
    financial_data_ok = bool(financials.get("report_date"))

    parts: dict[str, dict[str, float | None]] = {
        "ROE": {"score": _linear(roe, 0, 25), "weight": 20},
        "营收增速": {"score": _linear(revenue_growth, -20, 30), "weight": 10},
        "利润增速": {"score": _linear(profit_growth, -30, 40), "weight": 15},
        "负债率": {
            "score": 60.0
            if _is_financial(industry)
            else _reverse(debt, 10, 80),
            "weight": 10,
        },
        "毛利率": {"score": _linear(gross, 10, 60), "weight": 10},
        "经营现金流": {"score": _linear(cash, 0, 1.0), "weight": 5},
        "PE": {
            "score": 15.0
            if pe is not None and pe <= 0
            else _linear(pe, 5, 60) if pe is not None else 50.0,
            "weight": 10,
        },
        "PB": {
            "score": 20.0
            if pb is not None and pb <= 0
            else _linear(pb, 0.5, 8) if pb is not None else 50.0,
            "weight": 5,
        },
        "市值": {"score": _market_cap_score(market_cap_yi), "weight": 10},
        "流动性": {"score": _liquidity_score(turnover), "weight": 5},
    }
    # Valuation is better when PE/PB are low; _linear would reward high values.
    if pe is not None and pe > 0:
        parts["PE"]["score"] = _reverse(pe, 5, 60)
    if pb is not None and pb > 0:
        parts["PB"]["score"] = _reverse(pb, 0.5, 8)

    total = sum(
        float(part["score"] or 0) * float(part["weight"] or 0)
        for part in parts.values()
    ) / 100.0
    if not financial_data_ok:
        total -= 10.0
    name = str(quote.get("name") or "")
    if "ST" in name.upper() or "退" in name:
        total -= 25.0
    return _round(_clamp(total), 1) or 0.0, parts


def trend_score(tech: dict) -> float:
    if not tech:
        return 50.0
    score = 50.0
    if tech.get("above_ma20") is True:
        score += 15
    elif tech.get("above_ma20") is False:
        score -= 15
    if tech.get("above_ma60") is True:
        score += 15
    elif tech.get("above_ma60") is False:
        score -= 15
    ma20 = tech.get("ma20")
    ma60 = tech.get("ma60")
    if ma20 is not None and ma60 is not None:
        score += 10 if ma20 > ma60 else -10
    rsi14 = tech.get("rsi14")
    if rsi14 is not None:
        if 40 <= rsi14 <= 65:
            score += 10
        elif rsi14 > 75:
            score -= 10
        elif rsi14 < 30:
            score += 5
    ret20 = tech.get("ret_20d")
    if ret20 is not None:
        score += 10 if ret20 > 0 else -5
    return _round(_clamp(score), 1) or 0.0


def position_score(range_pct: float | None, from_high_1y: float | None, trend: str) -> float:
    if range_pct is None:
        return 50.0
    score = 100.0 - float(range_pct)
    if from_high_1y is not None and from_high_1y < -30 and trend in {"偏弱", "回调"}:
        score -= 10
    return _round(_clamp(score), 1) or 0.0


def risk_score(
    *,
    dd_1y: float | None,
    vol_1y: float | None,
    market_cap_yi: float | None,
    pe: float | None,
    name: str,
    stale_days: int,
    from_high_1y: float | None,
    trend: str,
) -> float:
    score = 100.0
    dd = abs(dd_1y) if dd_1y is not None else None
    if dd is not None:
        if dd > 30:
            score -= 30
        elif dd > 20:
            score -= 20
        elif dd > 15:
            score -= 10
    if vol_1y is not None:
        if vol_1y > 50:
            score -= 25
        elif vol_1y > 35:
            score -= 15
        elif vol_1y > 25:
            score -= 5
    if market_cap_yi is not None and market_cap_yi < 50:
        score -= 10
    if pe is not None:
        if pe < 0:
            score -= 15
        elif pe > 80:
            score -= 10
    if "ST" in name.upper() or "退" in name:
        score -= 30
    if stale_days > 5:
        score -= 20
    if from_high_1y is not None and from_high_1y < -40:
        score -= 10
    if trend == "偏弱":
        score -= 5
    return _round(_clamp(score), 1) or 0.0


def _quality_label(score: float) -> str:
    if score >= 80:
        return "质量强"
    if score >= 70:
        return "质量中上"
    if score >= 60:
        return "质量一般"
    return "质量弱"


def _position_label(score: float) -> str:
    if score >= 70:
        return "近一年低位"
    if score >= 40:
        return "近一年中位"
    return "近一年高位"


def _risk_label(score: float) -> str:
    if score >= 80:
        return "风险较低"
    if score >= 65:
        return "风险中等"
    if score >= 50:
        return "风险偏高"
    return "风险高"


def build_metrics(
    stock: dict,
    config: dict | None = None,
    today: date | None = None,
) -> dict:
    """Build explainable stock metrics from quote + K-line + financials.

    The numbers are descriptive, not a forecast. A score above 80 only means
    several factors point in the same direction; it is not a win probability.
    """
    config = config or {}
    today = today or date.today()
    quote = stock.get("quote") or {}
    financials = stock.get("financials") or {}
    kline = stock.get("kline") or []
    if not kline:
        raise ValueError(f"{stock.get('code')} K 线数据不足")

    rows: list[dict] = []
    for item in kline:
        try:
            day = date.fromisoformat(str(item.get("date")))
            close = float(item.get("close"))
        except (TypeError, ValueError):
            continue
        if day > today or close <= 0:
            continue
        rows.append({**item, "_day": day, "_close": close})
    if len(rows) < 2:
        raise ValueError(f"{stock.get('code')} K 线数据不足")

    rows.sort(key=lambda item: item["_day"])
    series = [(item["_day"], item["_close"]) for item in rows]
    latest_row = rows[-1]
    price = quote.get("price")
    if price is None or float(price) <= 0:
        price = latest_row["_close"]
    price = float(price)
    price_date = latest_row["_day"]
    daily_pct = latest_row.get("pct")
    if daily_pct is None:
        daily_pct = quote.get("pct")
    if daily_pct is None and len(series) >= 2 and series[-2][1] > 0:
        daily_pct = (series[-1][1] / series[-2][1] - 1) * 100

    risk_free = float(config.get("risk_free_rate", 1.5))
    ret_5d = total_return(series, 7)
    ret_20d = total_return(series, 30)
    ret_60d = total_return(series, 90)
    ret_120d = total_return(series, 180)
    ret_250d = total_return(series, 365)
    ret_750d = total_return(series, 1095)
    dd_1y = max_drawdown(series, 365)
    dd_3y = max_drawdown(series, 1095)
    vol_1y = volatility(series, 365)
    vol_3y = volatility(series, 1095)
    sharpe_1y = sharpe(series, 365, risk_free_rate=risk_free)
    sharpe_3y = sharpe(series, 1095, risk_free_rate=risk_free)
    ann_1y = annualized_return(series, 365)
    ann_3y = annualized_return(series, 1095)
    from_high_1y = drawdown_from_high(series, 365)
    from_high_3y = drawdown_from_high(series, 1095)
    low_1y, high_1y, range_pct = nav_range(series, 365)
    tech = price_technicals(series)
    stale_days = max(0, (today - price_date).days)
    latest_volume = latest_row.get("volume") or quote.get("volume")
    recent_volumes = [
        float(item.get("volume"))
        for item in rows[-21:-1]
        if item.get("volume") is not None
    ]
    volume_avg_20 = (
        sum(recent_volumes) / len(recent_volumes) if recent_volumes else None
    )
    volume_spike = (
        float(latest_volume) / volume_avg_20
        if latest_volume is not None and volume_avg_20 and volume_avg_20 > 0
        else None
    )
    market_cap_yi = (
        float(quote["market_cap"]) / 1e8
        if quote.get("market_cap") is not None
        else None
    )
    float_market_cap_yi = (
        float(quote["float_market_cap"]) / 1e8
        if quote.get("float_market_cap") is not None
        else None
    )
    name = str(quote.get("name") or stock.get("name") or stock.get("code"))

    quality, quality_parts = quality_score(quote, financials)
    trend = trend_score(tech)
    position = position_score(range_pct, from_high_1y, str(tech.get("trend") or ""))
    risk = risk_score(
        dd_1y=dd_1y,
        vol_1y=vol_1y,
        market_cap_yi=market_cap_yi,
        pe=quote.get("pe"),
        name=name,
        stale_days=stale_days,
        from_high_1y=from_high_1y,
        trend=str(tech.get("trend") or ""),
    )
    overall = quality * 0.60 + trend * 0.20 + risk * 0.20
    if len(series) < 250:
        overall -= 10
    elif len(series) < 750:
        overall -= 3
    overall = _round(_clamp(overall), 1) or 0.0

    return {
        "code": stock.get("code"),
        "name": name,
        "price": _round(price, 2),
        "price_date": price_date.isoformat(),
        "daily_pct": _round(daily_pct, 2),
        "change": _round(quote.get("change"), 2),
        "open": _round(quote.get("open") or latest_row.get("open"), 2),
        "high": _round(quote.get("high") or latest_row.get("high"), 2),
        "low": _round(quote.get("low") or latest_row.get("low"), 2),
        "prev_close": _round(quote.get("prev_close"), 2),
        "volume": _round(quote.get("volume") or latest_row.get("volume"), 2),
        "amount": _round(quote.get("amount") or latest_row.get("amount"), 2),
        "turnover": _round(quote.get("turnover") or latest_row.get("turnover"), 2),
        "volume_ratio": _round(quote.get("volume_ratio"), 2),
        "volume_avg_20": _round(volume_avg_20, 2),
        "volume_spike": _round(volume_spike, 2),
        "pe": _round(quote.get("pe"), 2),
        "pb": _round(quote.get("pb"), 2),
        "market_cap_yi": _round(market_cap_yi, 1),
        "float_market_cap_yi": _round(float_market_cap_yi, 1),
        "industry": quote.get("industry") or "",
        "region": quote.get("region") or "",
        "concepts": quote.get("concepts") or "",
        "ret_5d": _round(ret_5d, 2),
        "ret_20d": _round(ret_20d, 2),
        "ret_60d": _round(ret_60d, 2),
        "ret_120d": _round(ret_120d, 2),
        "ret_250d": _round(ret_250d, 2),
        "ret_750d": _round(ret_750d, 2),
        "ann_1y": _round(ann_1y, 2),
        "ann_3y": _round(ann_3y, 2),
        "dd_1y": _round(dd_1y, 2),
        "dd_3y": _round(dd_3y, 2),
        "vol_1y": _round(vol_1y, 2),
        "vol_3y": _round(vol_3y, 2),
        "sharpe_1y": _round(sharpe_1y, 2),
        "sharpe_3y": _round(sharpe_3y, 2),
        "from_high_1y": _round(from_high_1y, 2),
        "from_high_3y": _round(from_high_3y, 2),
        "range_low_1y": _round(low_1y, 2),
        "range_high_1y": _round(high_1y, 2),
        "range_pct_1y": _round(range_pct, 1),
        "tech": tech,
        "stale_days": stale_days,
        "history_days": len(series),
        "financials": financials,
        "quality_score": quality,
        "quality_parts": quality_parts,
        "trend_score": trend,
        "position_score": position,
        "risk_score": risk,
        "score": overall,
        "quality_label": _quality_label(quality),
        "position_label": _position_label(position),
        "risk_label": _risk_label(risk),
        "trend_label": tech.get("trend") or "数据不足",
        "data_ok": True,
    }
