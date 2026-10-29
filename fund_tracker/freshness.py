from __future__ import annotations

from datetime import date
from typing import Any

DEFAULTS = {
    "warn_days": 5,
    "hard_days": 7,
    "holdings_warn_days": 7,
    "hard_ratio": 0.8,
}


def _config(config: dict | None) -> dict:
    merged = dict(DEFAULTS)
    merged.update((config or {}).get("freshness") or {})
    return merged


def _age_days(value: Any, today: date) -> int | None:
    if not value:
        return None
    try:
        return (today - date.fromisoformat(str(value))).days
    except (ValueError, TypeError):
        return None


def assess(
    rows: list[dict],
    holdings: dict | None,
    config: dict | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Judge data freshness.

    level:
      ok    - nothing notable
      warn  - some data is stale, conclusions need caution
      stale - data is too old to publish normal conclusions
    """
    cfg = _config(config)
    today = today or date.today()
    warn_days = int(cfg["warn_days"])
    hard_days = int(cfg["hard_days"])
    holdings_warn_days = int(cfg["holdings_warn_days"])
    hard_ratio = float(cfg["hard_ratio"])

    reasons: list[str] = []
    warn_reasons: list[str] = []

    ages: list[int] = []
    missing = 0
    for row in rows or []:
        age = _age_days(row.get("nav_date"), today)
        if age is None:
            missing += 1
            continue
        ages.append(age)

    total = len(rows or [])
    stale_count = sum(1 for age in ages if age > warn_days)
    hard_count = sum(1 for age in ages if age > hard_days) + missing
    stale_ratio = hard_count / total if total else 0.0
    max_age = max(ages) if ages else None

    level = "ok"
    if total and stale_ratio >= hard_ratio:
        level = "stale"
        reasons.append(
            f"{hard_count}/{total} 只净值超过 {hard_days} 天未更新或缺失"
        )
    elif stale_count or missing:
        level = "warn"
        warn_reasons.append(
            f"{stale_count} 只净值超过 {warn_days} 天，{missing} 只缺少净值日期"
        )

    holdings_age = None
    holdings_loaded = bool((holdings or {}).get("loaded"))
    if holdings_loaded:
        holdings_age = _age_days((holdings or {}).get("as_of"), today)
        if holdings_age is None:
            warn_reasons.append("持仓缺少有效 as_of")
        elif holdings_age > holdings_warn_days:
            warn_reasons.append(f"持仓快照已 {holdings_age} 天未更新")

    if warn_reasons and level == "ok":
        level = "warn"
    reasons.extend(warn_reasons)

    return {
        "level": level,
        "reasons": reasons,
        "rows": total,
        "stale_count": stale_count,
        "missing": missing,
        "stale_ratio": stale_ratio,
        "max_age_days": max_age,
        "holdings_age_days": holdings_age,
        "warn_days": warn_days,
        "hard_days": hard_days,
    }


def banner(quality: dict) -> str:
    """Report banner for stale data; empty string when data is usable."""
    if quality.get("level") != "stale":
        return ""
    reason = "；".join(quality.get("reasons") or ["数据过期"])
    return (
        "> **数据不完整：本期不推送正常结论**\n"
        f"> {reason}。\n"
        "> 报告仅作留档；请先修复数据源或等待净值更新后再看结论。\n"
    )
