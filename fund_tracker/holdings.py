from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fund_tracker.config import BASE_DIR


def holdings_path(config: dict) -> Path:
    return BASE_DIR / str(config.get("holdings_file") or "data/holdings.json")


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_holdings(config: dict) -> dict:
    """Load the user's actual fund positions.

    The screenshot/statement only gives market value and holding P&L, not
    shares.  Keep this file optional: without it the reports stay a
    target-weight research report.
    """
    path = holdings_path(config)
    if not path.exists():
        return {
            "loaded": False,
            "items": [],
            "cash": 0.0,
            "as_of": None,
            "path": str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "loaded": False,
            "items": [],
            "cash": 0.0,
            "as_of": None,
            "path": str(path),
            "error": "持仓文件无法解析",
        }
    if not isinstance(payload, dict):
        payload = {}
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    cleaned = [item for item in items if isinstance(item, dict) and item.get("code")]
    cash = _number(payload.get("cash")) or 0.0
    market_total = sum(
        (_number(item.get("market_value")) or _number(item.get("current_value")) or 0.0)
        for item in cleaned
    )
    total_value = _number(payload.get("total_value"))
    if total_value is None or total_value <= 0:
        total_value = market_total + cash
    total_pnl = _number(payload.get("total_pnl"))
    if total_pnl is None:
        total_pnl = sum(_number(item.get("pnl")) or 0.0 for item in cleaned)
    daily_pnl = _number(payload.get("daily_pnl"))
    cost_value = total_value - total_pnl
    return {
        "loaded": bool(cleaned) or cash > 0,
        "items": cleaned,
        "cash": cash,
        "as_of": payload.get("as_of"),
        "source": payload.get("source"),
        "path": str(path),
        "total_value": total_value,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "cost_value": cost_value,
        "total_return_pct": (
            total_pnl / cost_value * 100 if cost_value > 0 else None
        ),
        "market_total": market_total,
    }


def attach_holdings(rows: list[dict], holdings: dict) -> dict:
    """Attach actual market value, P&L and actual weight to fund rows."""
    by_code = {str(row.get("code")): row for row in rows}
    total_value = float(holdings.get("total_value") or 0.0)
    if total_value <= 0:
        total_value = sum(
            (
                float(item.get("market_value") or item.get("current_value") or 0.0)
                for item in holdings.get("items") or []
            ),
        ) + float(holdings.get("cash") or 0.0)
    attached: list[dict] = []
    unmatched: list[str] = []

    for item in holdings.get("items") or []:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        row = by_code.get(code)
        if not row:
            unmatched.append(code)
            continue
        market_value = float(
            _number(item.get("market_value"))
            or _number(item.get("current_value"))
            or 0.0
        )
        pnl = _number(item.get("pnl"))
        cost_value = _number(item.get("cost_value"))
        if cost_value is None and pnl is not None:
            cost_value = market_value - pnl
        pnl_pct = None
        if pnl is not None and cost_value and cost_value > 0:
            pnl_pct = pnl / cost_value * 100
        elif pnl is not None and market_value - pnl > 0:
            pnl_pct = pnl / (market_value - pnl) * 100
        attached.append(
            {
                "row": row,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "daily_pct": _number(item.get("daily_pct")),
                "note": item.get("note"),
            }
        )

    for item in attached:
        row = item["row"]
        row["market_value"] = item["market_value"]
        row["current_value"] = item["market_value"]
        row["pnl"] = item["pnl"]
        row["pnl_pct"] = item["pnl_pct"]
        row["daily_pct_actual"] = item["daily_pct"]
        row["holdings_note"] = item["note"]
        row["actual_weight"] = (
            item["market_value"] / total_value if total_value > 0 else 0.0
        )

    loaded = bool(holdings.get("loaded"))
    for row in rows:
        row.setdefault("actual_weight", 0.0 if loaded else None)
        row.setdefault("market_value", 0.0 if loaded else None)
        row.setdefault("current_value", 0.0 if loaded else None)
        row.setdefault("pnl", None)
        row.setdefault("pnl_pct", None)

    return {
        **holdings,
        "total_value": total_value,
        "cash_weight": (
            float(holdings.get("cash") or 0.0) / total_value
            if total_value > 0
            else None
        ),
        "attached_count": len(attached),
        "unmatched": unmatched,
    }
