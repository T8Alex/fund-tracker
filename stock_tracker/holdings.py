from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fund_tracker.config import BASE_DIR


def holdings_path(config: dict) -> Path:
    return BASE_DIR / str(config.get("holdings_file") or "stock-data/holdings.json")


def load_holdings(config: dict) -> dict:
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
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []
    cash = payload.get("cash", 0.0) if isinstance(payload, dict) else 0.0
    try:
        cash = float(cash or 0.0)
    except (TypeError, ValueError):
        cash = 0.0
    cleaned = [item for item in items if isinstance(item, dict)]
    return {
        "loaded": bool(cleaned) or cash > 0,
        "items": cleaned,
        "cash": cash,
        "as_of": payload.get("as_of") if isinstance(payload, dict) else None,
        "path": str(path),
    }


def attach_holdings(rows: list[dict], holdings: dict) -> dict:
    """Attach actual shares, cost, value, P&L and weight to stock rows.

    The file is optional. Without it the report is a watchlist/target-weight
    report; action labels never pretend that a position exists.
    """
    by_code = {str(row.get("code")): row for row in rows}
    total_value = float(holdings.get("cash") or 0.0)
    attached: list[dict] = []
    unmatched: list[str] = []

    for item in holdings.get("items") or []:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        row = by_code.get(code)
        if not row:
            unmatched.append(code)
            try:
                total_value += float(item.get("current_value") or 0.0)
            except (TypeError, ValueError):
                pass
            continue
        price = row.get("price")
        shares = item.get("shares")
        try:
            shares = float(shares) if shares is not None else None
        except (TypeError, ValueError):
            shares = None
        cost_price = item.get("cost_price")
        try:
            cost_price = float(cost_price) if cost_price is not None else None
        except (TypeError, ValueError):
            cost_price = None
        cost_amount = item.get("cost_amount")
        try:
            cost_amount = float(cost_amount) if cost_amount is not None else None
        except (TypeError, ValueError):
            cost_amount = None
        current_value = item.get("current_value")
        try:
            current_value = float(current_value) if current_value is not None else None
        except (TypeError, ValueError):
            current_value = None

        if cost_amount is None and shares is not None and cost_price is not None:
            cost_amount = shares * cost_price
        if current_value is None and shares is not None and price is not None:
            current_value = shares * float(price)
        if current_value is None:
            current_value = 0.0
        total_value += current_value
        attached.append(
            {
                "row": row,
                "shares": shares,
                "cost_price": cost_price,
                "cost_amount": cost_amount,
                "current_value": current_value,
                "note": item.get("note"),
            }
        )

    for item in attached:
        row = item["row"]
        current_value = float(item["current_value"] or 0.0)
        cost_amount = item["cost_amount"]
        pnl = None
        pnl_pct = None
        if cost_amount is not None and cost_amount > 0:
            pnl = current_value - cost_amount
            pnl_pct = pnl / cost_amount * 100
        row["shares"] = item["shares"]
        row["cost_price"] = item["cost_price"]
        row["cost_amount"] = cost_amount
        row["current_value"] = current_value
        row["pnl"] = pnl
        row["pnl_pct"] = pnl_pct
        row["holdings_note"] = item["note"]
        row["actual_weight"] = (
            current_value / total_value if total_value > 0 else 0.0
        )

    for row in rows:
        row.setdefault("actual_weight", 0.0 if holdings.get("loaded") else None)
        row.setdefault("current_value", 0.0 if holdings.get("loaded") else None)
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
