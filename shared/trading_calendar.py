"""One home for date/weekday arithmetic (Phase 2 step 1, pure refactor).

This module only MOVES duplicated helpers; it does not change behavior:

- `next_weekday_date` keeps two call-site semantics behind a flag, because
  they were historically different and tests pin both:
  - learning.py: `max(0, trading_days)` → horizon 0 means "stay on start day"
  - report.py: `max(1, trading_days)` → horizon 0 still advances to the next
    weekday.
  The A-share-calendar behavior change (Phase 2 step 2) will pick one
  canonical semantic; the recommendation is horizon 0 = same day.

- Weekend skipping stays the only calendar knowledge here. Holidays are
  deliberately NOT modeled yet; that is the behavior change later.

Nothing here touches the network or the filesystem, so unit tests drive
everything with explicit dates.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# Only these two tables may appear in one SQL statement below (internal
# constants, never user input), so the dynamic table name stays safe.
_COUNTABLE_TABLES = frozenset({"nav", "prices"})

_DATASET_PATH = Path(__file__).resolve().parent / "data" / "ashare_holidays.json"
_dataset_cache: tuple[dict, set[str]] | None = None


def _dataset() -> tuple[dict, set[str]]:
    """Loaded holiday dataset + trading-day lookup set (lazy, cached)."""
    global _dataset_cache
    if _dataset_cache is None:
        try:
            raw = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _dataset_cache = (raw, set(raw.get("trading_days") or []))
    return _dataset_cache


def holidays_coverage() -> tuple[str | None, str | None]:
    """(coverage_start, coverage_end) ISO strings; (None, None) 没有数据集。"""
    raw, _ = _dataset()
    return raw.get("coverage_start"), raw.get("coverage_end")


def _first_of_next_month(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def last_weekday_of_month(day: date) -> date:
    """The last Mon–Fri day of `day`'s own month."""
    last = _first_of_next_month(day.year, day.month) - timedelta(days=1)
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return last


def next_month_last_weekday(day: date) -> date:
    """The last Mon–Fri day of the month AFTER `day`'s month."""
    year, month = day.year, day.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return last_weekday_of_month(date(year, month, 1))


def is_last_weekday(day: date) -> bool:
    return day == last_weekday_of_month(day)


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_trading_day(day: date) -> bool:
    """A-share trading day: covered range → exact (bar exists); beyond → weekend-only.

    覆盖范围外（数据集 coverage_end 之后）无法确知节假日，只能按周末近似；
    这是文档化的已知限制，doctor 会标出覆盖终点。
    """
    _, coverage_end = holidays_coverage()
    if coverage_end and day.isoformat() <= coverage_end:
        _, trading = _dataset()
        return day.isoformat() in trading
    return not is_weekend(day)


def is_holiday_weekday(day: date) -> bool:
    """工作日但在覆盖数据里是休市日（法定节假日换来的 A 股休市）。"""
    if is_weekend(day):
        return False
    raw, _ = _dataset()
    end = raw.get("coverage_end")
    if not end or day.isoformat() > end:
        return False  # 覆盖外不判定，回退"工作日=交易日"
    return day.isoformat() not in raw.get("trading_days", [])


def next_trading_date(
    start_date: str,
    trading_days: int,
    *,
    at_least_one: bool = False,
) -> str:
    """Advance `start_date` by `trading_days` A-share trading days.

    统一语义（Phase 2 step 2）：horizon 0 = 停留在 start_date 当天；
    `at_least_one=True` 是向后兼容层保留的旧 report 语义（0 → 下一个交易日）。
    """
    current = date.fromisoformat(str(start_date))
    remaining = max(0, int(trading_days))
    if at_least_one and remaining == 0:
        remaining = 1
    while remaining:
        current += timedelta(days=1)
        if is_trading_day(current):
            remaining -= 1
    return current.isoformat()


def last_trading_day_of_month(day: date) -> date:
    """Month-end walk-back to the last trading day (覆盖尾部如未确认则按周末近似)."""
    next_month_first = _first_of_next_month(day.year, day.month)
    cursor = next_month_first - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def trading_days_between(start_day: date | str, end_day: date | str) -> int:
    """Count trading days in `(start, end]`; coverage beyond end falls back to weekends."""
    if isinstance(start_day, str):
        start_day = date.fromisoformat(start_day)
    if isinstance(end_day, str):
        end_day = date.fromisoformat(end_day)
    raw, trading = _dataset()
    end = raw.get("coverage_end")
    count = 0
    cursor = start_day + timedelta(days=1)
    while cursor <= end_day:
        if end and cursor.isoformat() <= end:
            if cursor.isoformat() in trading:
                count += 1
        elif not is_weekend(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def next_weekday_date(
    start_date: str,
    trading_days: int,
    *,
    at_least_one: bool = False,
) -> str:
    """Advance `start_date` by `trading_days` weekdays, skipping weekends.

    - `at_least_one=False` (learning semantics): 0 → the start day itself.
    - `at_least_one=True` (report semantics): 0 → the next weekday.
    Negative horizons clamp to 0. Weekend-only: see module docstring.
    """
    current = date.fromisoformat(str(start_date))
    remaining = max(0, int(trading_days))
    if at_least_one and remaining == 0:
        remaining = 1
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


def last_n_weekdays(count: int = 12, *, today: date | None = None) -> list[date]:
    """The `count` most recent weekdays, oldest first (macro tables use this)."""
    cursor = today or date.today()
    days: list[date] = []
    while len(days) < max(0, count):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def count_distinct_dates_since(
    connection: sqlite3.Connection,
    table: str,
    start_date: str,
    end_date: str,
) -> int:
    """Count DISTINCT date values in `(start_date, end_date]`.

    Used to size 2/5/20-day lookbacks by rows that actually exist in the
    store (weekend-only approximation today; A-share calendar later).
    """
    if table not in _COUNTABLE_TABLES:
        raise ValueError(f"whitelist 只允许 nav/prices，收到 {table!r}")
    row = connection.execute(
        f"SELECT COUNT(DISTINCT date) FROM {table} "  # noqa: S608 - whitelisted
        "WHERE date > ? AND date <= ?",
        (start_date, end_date),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0
