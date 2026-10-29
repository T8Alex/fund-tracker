"""Derive market sections from a persisted index history.

``fetch_market_history`` / ``fetch_market_history_range`` always read the
*latest* index bars, so replaying a past report silently used today's numbers
(a 10-29 review generated on 11-05 described the 11-05 market).  These helpers
take a persisted ``{name: [daily bars]}`` series and slice it by date instead,
which keeps past reports reproducible without touching the fetch layer.

Bar shape matches ``fund_tracker.fetch.fetch_index_history``:
``{"date": "2026-10-29", "open": ..., "close": ..., "high": ..., "low": ...,
"pct": ...}``.
"""
from __future__ import annotations


def _day(value: object) -> str:
    return str(value or "")[:10]


def _bars_by_date(series: dict | None) -> list[tuple[str, list[dict]]]:
    """Return ``(name, bars)`` pairs sorted by date, skipping unusable rows."""
    pairs: list[tuple[str, list[dict]]] = []
    for name, bars in (series or {}).items():
        usable = [
            bar
            for bar in bars or []
            if isinstance(bar, dict) and bar.get("close") and _day(bar.get("date"))
        ]
        usable.sort(key=lambda bar: _day(bar.get("date")))
        pairs.append((str(name), usable))
    return pairs


def market_history(
    series: dict | None,
    count: int = 6,
    *,
    as_of: str | None = None,
) -> list[dict]:
    """Same output as ``fetch_market_history(client, count)``, from saved bars.

    ``count`` is the number of trading days involved (``count - 1`` intervals),
    mirroring the fetch layer.  ``as_of`` drops bars newer than the report date
    so a replay never sees data published after it.
    """
    window = max(1, int(count) - 1)
    result: list[dict] = []
    for name, bars in _bars_by_date(series):
        rows = [bar for bar in bars if not as_of or _day(bar.get("date")) <= as_of]
        if len(rows) < count:
            continue
        latest = rows[-1]
        base = rows[-count]
        change = (
            (latest["close"] / base["close"] - 1) * 100 if base.get("close") else None
        )
        result.append(
            {
                "name": name,
                "current": latest["close"],
                "pct": latest.get("pct"),
                "pct_window": change,
                "window_days": window,
                "date": _day(latest.get("date")),
            }
        )
    return result


def market_close(series: dict | None, as_of: str) -> list[dict]:
    """Build the daily index table from saved bars for one specific day.

    Used when a past date is replayed: the live snapshot describes today, so
    the report has to show that day's closes instead.  An index without a bar
    exactly on ``as_of`` (holiday, suspension) is skipped rather than carried
    over from an older day.
    """
    result: list[dict] = []
    for name, bars in _bars_by_date(series):
        row = next(
            (bar for bar in reversed(bars) if _day(bar.get("date")) == as_of),
            None,
        )
        if not row:
            continue
        result.append(
            {
                "name": name,
                "current": row["close"],
                "pct": row.get("pct"),
                "date": as_of,
            }
        )
    return result


def market_range(
    series: dict | None,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Same output as ``fetch_market_history_range``, from saved bars."""
    result: list[dict] = []
    for name, bars in _bars_by_date(series):
        window = [
            bar for bar in bars if start_date <= _day(bar.get("date")) <= end_date
        ]
        if len(window) < 2:
            continue
        latest = window[-1]
        base = window[0]
        change = (
            (latest["close"] / base["close"] - 1) * 100 if base.get("close") else None
        )
        result.append(
            {
                "name": name,
                "current": latest["close"],
                "pct": latest.get("pct"),
                "pct_window": change,
                "window_days": len(window) - 1,
                "start_date": _day(base.get("date")),
                "date": _day(latest.get("date")),
            }
        )
    return result
