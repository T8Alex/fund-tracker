"""Shared formatting and utility functions for both fund_tracker and stock_tracker."""
from __future__ import annotations


def fmt_pct(value: float | None, signed: bool = True, digits: int = 2) -> str:
    if value is None:
        return "—"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.{digits}f}%"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _weight_text(value: float | None, missing: str = "—") -> str:
    if value is None:
        return missing
    pct = float(value) * 100
    return f"{pct:.1f}%"


def _period_return(row: dict) -> float | None:
    for key in ("ret_window", "ret_5d", "ret_20d"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _period_text(row: dict) -> str:
    days = int(row.get("period_days") or 2)
    return f"{days} 个交易日"


ACTION_ORDER = {
    "分批买入/持有": 0,
    "小仓试探": 1,
    "持有": 2,
    "观察": 3,
    "复核": 4,
    "减仓/复核": 5,
    "止盈/减仓": 6,
    "出货/复核": 7,
    "剔除": 8,
}
