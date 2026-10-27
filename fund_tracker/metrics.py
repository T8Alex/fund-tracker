from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

CN_TZ = timezone(timedelta(hours=8))

CATEGORY_ORDER = ["短债", "纯债", "二级债/固收+", "红利", "宽基", "权益", "转债", "其他"]

CATEGORY_ALIASES = {
    "二级债": "二级债/固收+",
    "固收+": "二级债/固收+",
    "固收": "二级债/固收+",
    "偏债": "二级债/固收+",
}


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    return CATEGORY_ALIASES.get(category.strip(), category.strip())


def to_date(timestamp_ms: Any) -> date | None:
    try:
        return datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=CN_TZ).date()
    except (TypeError, ValueError, OSError):
        return None


def parse_net_worth(items: list | None) -> list[tuple[date, float, float | None]]:
    result: dict[date, tuple[float, float | None]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        day = to_date(item.get("x"))
        value = item.get("y")
        if day is None or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        daily = item.get("equityReturn")
        try:
            daily = float(daily) if daily is not None else None
        except (TypeError, ValueError):
            daily = None
        result[day] = (value, daily)
    return sorted((day, value, daily) for day, (value, daily) in result.items())


def parse_ac_worth(items: list | None) -> list[tuple[date, float]]:
    result: dict[date, float] = {}
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        day = to_date(item[0])
        value = item[1]
        if day is None or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        result[day] = value
    return sorted(result.items())


def slice_series(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> list[tuple[date, float]]:
    if not series:
        return []
    end = end or series[-1][0]
    start = end - timedelta(days=days)
    return [(day, value) for day, value in series if start <= day <= end]


def total_return(series: list[tuple[date, float]], days: int, end: date | None = None) -> float | None:
    sub = slice_series(series, days, end)
    if len(sub) < 2 or sub[0][1] <= 0:
        return None
    return (sub[-1][1] / sub[0][1] - 1) * 100


def annualized_return(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> float | None:
    sub = slice_series(series, days, end)
    if len(sub) < 2 or sub[0][1] <= 0:
        return None
    elapsed = (sub[-1][0] - sub[0][0]).days
    if elapsed <= 0:
        return None
    total = sub[-1][1] / sub[0][1]
    if total <= 0:
        return None
    return (total ** (365.25 / elapsed) - 1) * 100


def max_drawdown(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> float | None:
    sub = slice_series(series, days, end)
    if len(sub) < 2:
        return None
    peak = sub[0][1]
    worst = 0.0
    for _, value in sub:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1) * 100)
    return worst


def volatility(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> float | None:
    sub = slice_series(series, days, end)
    if len(sub) < 3:
        return None
    returns = []
    for index in range(1, len(sub)):
        previous = sub[index - 1][1]
        if previous > 0:
            returns.append(sub[index][1] / previous - 1)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100


def sharpe(
    series: list[tuple[date, float]],
    days: int,
    risk_free_rate: float = 1.5,
    end: date | None = None,
) -> float | None:
    annual = annualized_return(series, days, end)
    vol = volatility(series, days, end)
    if annual is None or vol is None or vol <= 0:
        return None
    return (annual - risk_free_rate) / vol


def calmar(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> float | None:
    annual = annualized_return(series, days, end)
    drawdown = max_drawdown(series, days, end)
    if annual is None or drawdown is None or drawdown >= 0:
        return None
    return annual / abs(drawdown)


def drawdown_from_high(
    series: list[tuple[date, float]],
    days: int,
    end: date | None = None,
) -> float | None:
    sub = slice_series(series, days, end)
    if len(sub) < 2:
        return None
    peak = max(value for _, value in sub)
    if peak <= 0:
        return None
    return (sub[-1][1] / peak - 1) * 100


def nav_range(
    series: list[tuple[date, float]],
    days: int = 365,
    end: date | None = None,
) -> tuple[float | None, float | None, float | None]:
    """Return (1-year low, 1-year high, current percentile 0-100)."""
    sub = slice_series(series, days, end)
    if len(sub) < 2:
        return None, None, None
    low = min(value for _, value in sub)
    high = max(value for _, value in sub)
    current = sub[-1][1]
    if high <= low:
        return round(low, 4), round(high, 4), 50.0
    percentile = (current - low) / (high - low) * 100
    return round(low, 4), round(high, 4), round(max(0.0, min(100.0, percentile)), 1)


def max_daily_move(series: list[tuple[date, float]], days: int = 365) -> float | None:
    sub = slice_series(series, days)
    if len(sub) < 2:
        return None
    worst = 0.0
    for index in range(1, len(sub)):
        previous = sub[index - 1][1]
        if previous > 0:
            worst = max(worst, abs(sub[index][1] / previous - 1) * 100)
    return worst


def parse_manager(managers: list | None) -> dict:
    if not managers:
        return {}
    first = managers[0] if isinstance(managers[0], dict) else {}
    names = [
        str(item.get("name", "")).strip()
        for item in managers
        if isinstance(item, dict) and item.get("name")
    ]
    work_time = str(first.get("workTime", ""))
    years = 0.0
    match = re.search(r"(\d+)\s*年(?:又\s*(\d+)\s*天)?", work_time)
    if match:
        years = float(match.group(1))
        if match.group(2):
            years += float(match.group(2)) / 365.0
    fund_size = 0.0
    match = re.search(r"([\d.]+)\s*亿", str(first.get("fundSize", "")))
    if match:
        fund_size = float(match.group(1))
    return {
        "name": "、".join(names) or str(first.get("name", "")),
        "years": round(years, 2),
        "fund_size_yi": fund_size,
        "star": first.get("star"),
        "work_time_raw": work_time,
    }


def parse_scale(scale: Any) -> dict:
    if not isinstance(scale, dict):
        return {}
    categories = scale.get("categories") or []
    series = scale.get("series") or []
    values = []
    for item in series:
        if isinstance(item, dict) and item.get("y") is not None:
            try:
                values.append(float(item["y"]))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (int, float)):
            values.append(float(item))
    if not values:
        return {}
    return {
        "latest_yi": values[-1],
        "prev_yi": values[-2] if len(values) > 1 else None,
        "date": categories[-1] if categories else None,
    }


def parse_allocation(allocation: Any) -> dict:
    if not isinstance(allocation, dict):
        return {}
    result: dict[str, float] = {}
    for item in allocation.get("series") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        values = [value for value in (item.get("data") or []) if value is not None]
        if not values:
            continue
        try:
            latest = float(values[-1])
        except (TypeError, ValueError):
            continue
        if "股票" in name:
            result["stock"] = latest
        elif "债券" in name:
            result["bond"] = latest
        elif "现金" in name:
            result["cash"] = latest
    return result


def parse_rank(history: list | None) -> dict:
    if not history:
        return {}
    last = history[-1]
    if not isinstance(last, dict):
        return {}
    try:
        rank = int(float(last.get("y")))
        total = int(float(last.get("sc")))
    except (TypeError, ValueError):
        return {}
    if rank <= 0 or total <= 0:
        return {}
    return {
        "rank": rank,
        "total": total,
        "pct": round(rank / total * 100, 1),
    }


def parse_evaluation(evaluation: Any) -> dict:
    if not isinstance(evaluation, dict):
        return {}
    average = evaluation.get("avr")
    try:
        average = float(average)
    except (TypeError, ValueError):
        average = None
    return {"avg": average, "data": evaluation.get("data")}


def classify_category(name: str, fund_type: str | None, allocation: dict) -> str:
    name = name or ""
    fund_type = fund_type or ""
    stock = float(allocation.get("stock") or 0)

    if "短债" in name or "超短债" in name or "中短债" in name:
        return "短债"
    if "可转债" in name or "转债" in name:
        return "转债"
    if "红利" in name or "低波" in name:
        return "红利"
    broad_keywords = ["沪深300", "中证500", "中证A500", "上证50", "中证800", "创业板", "科创50", "中证1000"]
    if any(keyword in name for keyword in broad_keywords):
        return "宽基"

    if "债券" in fund_type:
        if "二级" in name or stock > 0.5:
            return "二级债/固收+"
        return "纯债"
    if "混合" in fund_type:
        if stock <= 35:
            return "二级债/固收+"
        return "权益"
    if "股票" in fund_type or "指数" in fund_type:
        return "权益"
    if "债券" in name:
        return "纯债"
    return "其他"


def _linear(value: float | None, low: float, high: float) -> float:
    if value is None or high <= low:
        return 0.0
    score = (float(value) - low) / (high - low) * 100
    return max(0.0, min(100.0, score))


def _size_score(size_yi: float | None) -> float:
    if size_yi is None:
        return 40.0
    size = float(size_yi)
    if size < 2:
        return 0.0
    if size < 5:
        return 50 + (size - 2) / 3 * 30
    if size <= 50:
        return 100.0
    if size <= 100:
        return 80.0
    if size <= 200:
        return 40.0
    return 10.0


def _fit_score(drawdown: float | None, tolerance: float) -> float:
    if drawdown is None:
        return 40.0
    dd = abs(float(drawdown))
    if dd <= 8:
        return 100.0
    if dd <= 12:
        return 85.0
    if dd <= tolerance:
        return 70.0
    if dd <= tolerance + 5:
        return 45.0
    if dd <= tolerance + 15:
        return 15.0
    return 0.0


WEIGHTS = {
    "短债": {"ret": 20, "dd": 30, "sharpe": 20, "manager": 10, "size": 10, "fee": 10},
    "纯债": {"ret": 25, "dd": 25, "sharpe": 15, "manager": 15, "size": 10, "fee": 10},
    "二级债/固收+": {"ret": 25, "dd": 25, "sharpe": 15, "manager": 15, "size": 10, "fee": 10},
    "红利": {"ret": 20, "dd": 30, "sharpe": 20, "manager": 10, "size": 10, "fee": 10},
    "宽基": {"ret": 20, "dd": 30, "sharpe": 20, "manager": 10, "size": 10, "fee": 10},
    "权益": {"ret": 20, "dd": 30, "sharpe": 20, "manager": 10, "size": 10, "fee": 10},
    "转债": {"ret": 20, "dd": 30, "sharpe": 20, "manager": 10, "size": 10, "fee": 10},
    "其他": {"ret": 20, "dd": 25, "sharpe": 15, "manager": 15, "size": 15, "fee": 10},
}

RANGES = {
    "短债": {"ret": (1.0, 3.5), "dd": (0.3, 3.0), "sharpe": (0.0, 2.0)},
    "纯债": {"ret": (1.5, 5.0), "dd": (0.5, 6.0), "sharpe": (0.0, 2.0)},
    "二级债/固收+": {"ret": (2.0, 7.0), "dd": (2.0, 12.0), "sharpe": (0.0, 1.5)},
    "红利": {"ret": (2.0, 10.0), "dd": (8.0, 35.0), "sharpe": (0.0, 1.2)},
    "宽基": {"ret": (2.0, 10.0), "dd": (10.0, 40.0), "sharpe": (0.0, 1.2)},
    "权益": {"ret": (2.0, 12.0), "dd": (15.0, 50.0), "sharpe": (0.0, 1.0)},
    "转债": {"ret": (2.0, 12.0), "dd": (10.0, 40.0), "sharpe": (0.0, 1.0)},
    "其他": {"ret": (1.0, 8.0), "dd": (5.0, 30.0), "sharpe": (0.0, 1.2)},
}


def score_fund(metrics: dict, category: str, tolerance: float = 15.0) -> tuple[float, dict]:
    category = category if category in WEIGHTS else "其他"
    weights = WEIGHTS[category]
    ranges = RANGES[category]

    if category == "短债":
        annual = metrics.get("ret_1y")
        drawdown = metrics.get("dd_1y")
        ratio = metrics.get("sharpe_1y")
    else:
        annual = metrics.get("ann_3y")
        drawdown = metrics.get("dd_3y")
        ratio = metrics.get("sharpe_3y")
        if annual is None:
            annual = metrics.get("ret_1y")
            drawdown = metrics.get("dd_1y")
            ratio = metrics.get("sharpe_1y")
            metrics["score_used_1y_fallback"] = True

    ret_score = _linear(annual, *ranges["ret"])
    dd_score = 100.0 - _linear(abs(drawdown) if drawdown is not None else None, *ranges["dd"])
    sharpe_score = _linear(ratio, *ranges["sharpe"])
    manager_score = _linear(metrics.get("manager_years"), 0, 6)
    size_score = _size_score(metrics.get("scale_yi"))

    annual_fee = metrics.get("annual_fee_pct")
    fee_score = 100.0 - _linear(annual_fee, 0.1, 1.5) if annual_fee is not None else 50.0

    parts = {
        "return": round(ret_score * weights["ret"] / 100, 2),
        "drawdown": round(dd_score * weights["dd"] / 100, 2),
        "sharpe": round(sharpe_score * weights["sharpe"] / 100, 2),
        "manager": round(manager_score * weights["manager"] / 100, 2),
        "size": round(size_score * weights["size"] / 100, 2),
        "fee": round(fee_score * weights["fee"] / 100, 2),
    }
    quality = sum(parts.values())
    fit = _fit_score(drawdown, tolerance)
    total = quality * 0.75 + fit * 0.25

    history_years = metrics.get("history_years") or 0
    if history_years < 1:
        total -= 20
    elif history_years < 3:
        total -= 5
    total = max(0.0, min(100.0, total))

    parts["quality"] = round(quality, 2)
    parts["fit"] = round(fit, 2)
    return round(total, 1), parts




def moving_average(
    series: list[tuple[date, float]],
    window: int,
) -> float | None:
    if window <= 0 or len(series) < window:
        return None
    values = [value for _, value in series[-window:]]
    return sum(values) / len(values)


def rsi(
    series: list[tuple[date, float]],
    window: int = 14,
) -> float | None:
    if window <= 0 or len(series) < window + 1:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(len(series) - window, len(series)):
        change = series[index][1] - series[index - 1][1]
        if change >= 0:
            gains += change
        else:
            losses += -change
    average_gain = gains / window
    average_loss = losses / window
    if average_loss <= 0:
        return 100.0
    rs = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + rs)


def price_technicals(
    series: list[tuple[date, float]],
) -> dict:
    """Simple, explainable K-line features. Not a prediction."""
    if len(series) < 2:
        return {}
    current = series[-1][1]
    ma20 = moving_average(series, 20)
    ma60 = moving_average(series, 60)
    rsi14 = rsi(series, 14)
    base20 = series[-21][1] if len(series) >= 21 else series[0][1]
    base60 = series[-61][1] if len(series) >= 61 else series[0][1]
    high60 = max(value for _, value in series[-60:]) if len(series) >= 2 else current
    ret_20d = (current / base20 - 1) * 100 if base20 > 0 else None
    ret_60d = (current / base60 - 1) * 100 if base60 > 0 else None
    drawdown_60d = (current / high60 - 1) * 100 if high60 > 0 else None
    if ma20 is not None and ma60 is not None:
        if current > ma20 > ma60:
            trend = "偏强"
        elif current > ma20 and ma20 <= ma60:
            trend = "反弹"
        elif current < ma20 and current > ma60:
            trend = "回调"
        else:
            trend = "偏弱"
    else:
        trend = "数据不足"
    return {
        "ma20": _round(ma20, 4),
        "ma60": _round(ma60, 4),
        "rsi14": _round(rsi14, 1),
        "ret_20d": _round(ret_20d),
        "ret_60d": _round(ret_60d),
        "drawdown_60d": _round(drawdown_60d),
        "above_ma20": None if ma20 is None else current > ma20,
        "above_ma60": None if ma60 is None else current > ma60,
        "trend": trend,
    }


def build_metrics(
    fund: dict,
    profile: dict,
    config: dict,
    today: date | None = None,
) -> dict:
    today = today or date.today()
    risk_free = float(config.get("risk_free_rate", 1.5))
    tolerance = float(config.get("max_drawdown_tolerance", 15.0))

    net_series_full = parse_net_worth(fund.get("net_worth"))
    ac_series = parse_ac_worth(fund.get("ac_worth"))
    series = ac_series if len(ac_series) >= 2 else [(day, value) for day, value, _ in net_series_full]
    if len(series) < 2:
        raise ValueError(f"{fund.get('code')} 净值数据不足")

    nav_series = [(day, value) for day, value, _ in net_series_full] or series
    latest_nav = nav_series[-1][1]
    nav_date = nav_series[-1][0]
    daily_pct = net_series_full[-1][2] if net_series_full and net_series_full[-1][2] is not None else None
    if daily_pct is None and len(nav_series) >= 2 and nav_series[-2][1] > 0:
        daily_pct = (nav_series[-1][1] / nav_series[-2][1] - 1) * 100

    manager = parse_manager(fund.get("managers"))
    scale = parse_scale(fund.get("scale"))
    allocation = parse_allocation(fund.get("allocation"))
    rank = parse_rank(fund.get("rank_history"))
    evaluation = parse_evaluation(fund.get("evaluation"))

    category = classify_category(
        fund.get("name", ""),
        profile.get("fund_type"),
        allocation,
    )

    manage_fee = profile.get("manage_fee")
    custody_fee = profile.get("custody_fee")
    annual_fee = None
    if manage_fee is not None or custody_fee is not None:
        annual_fee = (manage_fee or 0) + (custody_fee or 0)

    history_years = round((series[-1][0] - series[0][0]).days / 365.25, 2)
    # Price ranges must use unit NAV, not accumulated NAV, otherwise funds
    # with dividends show ranges far above the quoted NAV.
    range_low_1y, range_high_1y, range_pct_1y = nav_range(nav_series, 365)
    metrics = {
        "code": fund.get("code"),
        "name": fund.get("name"),
        "nav": round(latest_nav, 4),
        "nav_date": nav_date.isoformat(),
        "stale_days": max(0, (today - nav_date).days),
        "daily_pct": None if daily_pct is None else round(daily_pct, 2),
        "ret_1w": _round(total_return(series, 7)),
        "ret_1m": _round(total_return(series, 30)),
        "ret_3m": _round(total_return(series, 91)),
        "ret_6m": _round(total_return(series, 182)),
        "ret_1y": _round(total_return(series, 365)),
        "ret_3y": _round(total_return(series, 1095)),
        "ann_3y": _round(annualized_return(series, 1095)),
        "dd_1y": _round(max_drawdown(series, 365)),
        "dd_3y": _round(max_drawdown(series, 1095)),
        "vol_1y": _round(volatility(series, 365)),
        "vol_3y": _round(volatility(series, 1095)),
        "sharpe_1y": _round(sharpe(series, 365, risk_free), 2),
        "sharpe_3y": _round(sharpe(series, 1095, risk_free), 2),
        "calmar_3y": _round(calmar(series, 1095), 2),
        "from_high_1y": _round(drawdown_from_high(series, 365)),
        "max_daily_move_1y": _round(max_daily_move(series, 365)),
        "max_daily_move_3y": _round(max_daily_move(series, 1095)),
        "max_daily_move_all": _round(max_daily_move(series, 100000)),
        "range_low_1y": range_low_1y,
        "range_high_1y": range_high_1y,
        "range_pct_1y": range_pct_1y,
        "history_years": history_years,
        "manager": manager,
        "manager_name": manager.get("name", ""),
        "manager_years": manager.get("years"),
        "scale": scale,
        "scale_yi": scale.get("latest_yi"),
        "allocation": allocation,
        "rank": rank,
        "rank_pct": rank.get("pct"),
        "evaluation": evaluation,
        "category": category,
        "fund_type": profile.get("fund_type"),
        "inception": profile.get("inception"),
        "buy_fee_pct": profile.get("buy_fee"),
        "manage_fee_pct": manage_fee,
        "custody_fee_pct": custody_fee,
        "annual_fee_pct": annual_fee,
        "redeem_fee_max_pct": profile.get("redeem_fee_max"),
        "benchmark": profile.get("benchmark"),
    }
    tech = price_technicals(series)
    metrics["tech"] = tech
    metrics["ma20"] = tech.get("ma20")
    metrics["ma60"] = tech.get("ma60")
    metrics["rsi14"] = tech.get("rsi14")
    metrics["ret_20d"] = tech.get("ret_20d")
    metrics["ret_60d"] = tech.get("ret_60d")
    metrics["trend"] = tech.get("trend")
    score, parts = score_fund(metrics, category, tolerance)
    metrics["score"] = score
    metrics["score_parts"] = parts
    return metrics


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)
