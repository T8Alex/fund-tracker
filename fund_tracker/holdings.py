from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fund_tracker.config import BASE_DIR

SNAPSHOT_FIELDS = ("code", "market_value", "pnl")


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

    # invested_ratio: fraction of total portfolio that is invested (vs cash).
    # Used to bring target_weight (stored as % of invested capital) onto the
    # same scale as actual_weight (% of total portfolio including cash).
    market_total = sum(
        (float(item.get("market_value") or item.get("current_value") or 0.0))
        for item in holdings.get("items") or []
    )
    invested_ratio = market_total / total_value if total_value > 0 else 1.0

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
        # Align target_weight to the same denominator (total portfolio incl. cash)
        tw = row.get("target_weight")
        if tw is not None:
            row["target_weight"] = tw * invested_ratio

    loaded = bool(holdings.get("loaded"))
    for row in rows:
        row.setdefault("actual_weight", 0.0 if loaded else None)
        row.setdefault("market_value", 0.0 if loaded else None)
        row.setdefault("current_value", 0.0 if loaded else None)
        row.setdefault("pnl", None)
        row.setdefault("pnl_pct", None)
        # Also align target_weight for rows without holdings data
        tw = row.get("target_weight")
        if tw is not None and not loaded:
            row["target_weight"] = tw * invested_ratio

    return {
        **holdings,
        "total_value": total_value,
        "cash_weight": (
            float(holdings.get("cash") or 0.0) / total_value
            if total_value > 0
            else None
        ),
        "invested_ratio": invested_ratio,
        "attached_count": len(attached),
        "unmatched": unmatched,
    }


def ledger_path(config: dict) -> Path:
    """Snapshot ledger lives next to holdings.json."""
    return holdings_path(config).with_name("holdings_ledger.jsonl")


def _snapshot_record(holdings: dict) -> dict:
    items = []
    for item in holdings.get("items") or []:
        items.append(
            {
                "code": str(item.get("code") or ""),
                "market_value": _number(item.get("market_value"))
                or _number(item.get("current_value"))
                or 0.0,
                "pnl": _number(item.get("pnl")),
            }
        )
    return {
        "as_of": holdings.get("as_of"),
        "total_value": float(holdings.get("total_value") or 0.0),
        "market_total": float(holdings.get("market_total") or 0.0),
        "cash": float(holdings.get("cash") or 0.0),
        "source": holdings.get("source"),
        "items": items,
    }


def load_holdings_ledger(config: dict) -> list[dict]:
    path = ledger_path(config)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("as_of"):
            records.append(payload)
    records.sort(key=lambda row: str(row.get("as_of")))
    return records


def append_holdings_snapshot(config: dict, holdings: dict) -> dict | None:
    """Append one snapshot per as_of date; returns it when appended."""
    if not holdings.get("loaded") or not holdings.get("as_of"):
        return None
    record = _snapshot_record(holdings)
    if record["total_value"] <= 0:
        return None
    if any(
        str(row.get("as_of")) == str(record["as_of"])
        for row in load_holdings_ledger(config)
    ):
        return None
    path = ledger_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return record


def snapshot_performance(records: list[dict]) -> dict | None:
    """Rough return/drawdown from snapshots; not strict TWR."""
    series = [
        (str(row.get("as_of")), float(row.get("total_value") or 0.0))
        for row in records
        if float(row.get("total_value") or 0.0) > 0
    ]
    if len(series) < 2:
        return None
    first_date, first_value = series[0]
    last_date, last_value = series[-1]
    peak = series[0][1]
    max_drawdown = 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
    return {
        "count": len(series),
        "first_date": first_date,
        "first_value": first_value,
        "last_date": last_date,
        "last_value": last_value,
        "return_pct": (last_value / first_value - 1) * 100 if first_value else None,
        "max_drawdown_pct": max_drawdown,
    }
