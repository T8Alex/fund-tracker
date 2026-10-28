from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from fund_tracker.learning import ensure_learning_schema
from shared.trading_calendar import count_distinct_dates_since

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    role TEXT,
    target_weight REAL,
    note TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    close REAL,
    high REAL,
    low REAL,
    volume REAL,
    amount REAL,
    pct REAL,
    turnover REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_code_date ON prices(code, date);
CREATE TABLE IF NOT EXISTS snapshots (
    code TEXT NOT NULL,
    run_date TEXT NOT NULL,
    metrics TEXT NOT NULL,
    score REAL,
    PRIMARY KEY (code, run_date)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    code TEXT,
    name TEXT,
    level TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    review_date TEXT PRIMARY KEY,
    period_start TEXT,
    period_end TEXT,
    created_at TEXT NOT NULL,
    summary TEXT,
    content_path TEXT
);
CREATE TABLE IF NOT EXISTS summary_reports (
    kind TEXT NOT NULL,
    period_key TEXT NOT NULL,
    report_date TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    created_at TEXT NOT NULL,
    summary TEXT,
    content_path TEXT,
    PRIMARY KEY (kind, period_key)
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            ensure_learning_schema(connection)

    def upsert_stock(
        self,
        code: str,
        name: str,
        category: str | None = None,
        role: str | None = None,
        target_weight: float | None = None,
        note: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO stocks(code, name, category, role, target_weight, note, added_at)
                VALUES(?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    category=COALESCE(excluded.category, stocks.category),
                    role=COALESCE(excluded.role, stocks.role),
                    target_weight=COALESCE(excluded.target_weight, stocks.target_weight),
                    note=COALESCE(excluded.note, stocks.note)
                """,
                (code, name, category, role, target_weight, note),
            )

    def list_stocks(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stocks ORDER BY COALESCE(target_weight, 0) DESC, code"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_stock(self, code: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM stocks WHERE code=?", (str(code),)
            ).fetchone()
        return dict(row) if row else None

    def remove_stock(self, code: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM stocks WHERE code=?", (str(code),)
            )
        return cursor.rowcount > 0

    def save_prices(self, code: str, rows: list[dict]) -> None:
        if not rows:
            return
        values = []
        for row in rows:
            day = str(row.get("date") or "")
            close = row.get("close")
            if not day or close is None:
                continue
            values.append(
                (
                    str(code),
                    day,
                    row.get("open"),
                    close,
                    row.get("high"),
                    row.get("low"),
                    row.get("volume"),
                    row.get("amount"),
                    row.get("pct"),
                    row.get("turnover"),
                )
            )
        if not values:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO prices(code, date, open, close, high, low, volume, amount, pct, turnover)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, date) DO UPDATE SET
                    open=excluded.open,
                    close=excluded.close,
                    high=excluded.high,
                    low=excluded.low,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    pct=excluded.pct,
                    turnover=excluded.turnover
                """,
                values,
            )

    def save_snapshot(self, code: str, run_date: str, metrics: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(code, run_date, metrics, score)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(code, run_date) DO UPDATE SET
                    metrics=excluded.metrics,
                    score=excluded.score
                """,
                (
                    str(code),
                    run_date,
                    json.dumps(metrics, ensure_ascii=False),
                    metrics.get("score"),
                ),
            )

    def get_snapshot(self, code: str, run_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metrics FROM snapshots WHERE code=? AND run_date=?",
                (str(code), run_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

    def get_previous_snapshot(self, code: str, before_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT metrics FROM snapshots
                WHERE code=? AND run_date < ?
                ORDER BY run_date DESC LIMIT 1
                """,
                (str(code), before_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

    def get_snapshots(self, run_date: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT metrics FROM snapshots WHERE run_date=? ORDER BY code",
                (run_date,),
            ).fetchall()
        return [json.loads(row["metrics"]) for row in rows]

    def get_snapshot_on_or_before(self, code: str, end_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT metrics FROM snapshots
                WHERE code=? AND run_date<=?
                ORDER BY run_date DESC LIMIT 1
                """,
                (str(code), end_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

    def save_alerts(self, run_date: str, alerts: list[dict]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM alerts WHERE run_date=?", (run_date,))
            connection.executemany(
                """
                INSERT INTO alerts(run_date, code, name, level, message)
                VALUES(?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_date,
                        alert.get("code"),
                        alert.get("name"),
                        alert.get("level"),
                        alert.get("message"),
                    )
                    for alert in alerts
                ],
            )

    def get_alerts(self, run_date: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT code, name, level, message FROM alerts WHERE run_date=? ORDER BY id",
                (run_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_alerts_on_or_before(self, end_date: str) -> list[dict]:
        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT MAX(run_date) AS day FROM runs WHERE run_date<=?",
                (end_date,),
            ).fetchone()
            latest = run_row["day"] if run_row else None
            if not latest:
                alert_row = connection.execute(
                    "SELECT MAX(run_date) AS day FROM alerts WHERE run_date<=?",
                    (end_date,),
                ).fetchone()
                latest = alert_row["day"] if alert_row else None
            if not latest:
                return []
            rows = connection.execute(
                "SELECT code, name, level, message FROM alerts WHERE run_date=? ORDER BY id",
                (latest,),
            ).fetchall()
        return [dict(item) for item in rows]

    def save_run(self, run_date: str, status: str, summary: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(run_date, created_at, status, summary)
                VALUES(?, datetime('now', 'localtime'), ?, ?)
                ON CONFLICT(run_date) DO UPDATE SET
                    created_at=datetime('now', 'localtime'),
                    status=excluded.status,
                    summary=excluded.summary
                """,
                (run_date, status, summary),
            )

    def get_run(self, run_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_date=?", (run_date,)
            ).fetchone()
        return dict(row) if row else None

    def get_price_window(self, code: str, end_date: str, count: int = 6) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT date, close, pct, turnover FROM prices
                WHERE code=? AND date<=?
                ORDER BY date DESC LIMIT ?
                """,
                (str(code), end_date, count),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_price_on_or_before(self, code: str, end_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT date, close, pct, turnover FROM prices
                WHERE code=? AND date<=?
                ORDER BY date DESC LIMIT 1
                """,
                (str(code), end_date),
            ).fetchone()
        return dict(row) if row else None

    def get_price_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT date, close, pct, turnover FROM prices
                WHERE code=? AND date>=? AND date<=?
                ORDER BY date
                """,
                (str(code), start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_trading_days_since(self, start_date: str, end_date: str) -> int:
        with self._connect() as connection:
            return count_distinct_dates_since(connection, "prices", start_date, end_date)

    def latest_price_date(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(date) AS day FROM prices"
            ).fetchone()
        return str(row["day"]) if row and row["day"] else None

    def save_review(
        self,
        review_date: str,
        period_start: str,
        period_end: str,
        summary: str,
        content_path: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reviews(review_date, period_start, period_end, created_at, summary, content_path)
                VALUES(?, ?, ?, datetime('now', 'localtime'), ?, ?)
                ON CONFLICT(review_date) DO UPDATE SET
                    period_start=excluded.period_start,
                    period_end=excluded.period_end,
                    created_at=datetime('now', 'localtime'),
                    summary=excluded.summary,
                    content_path=excluded.content_path
                """,
                (review_date, period_start, period_end, summary, content_path),
            )

    def get_last_review(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews ORDER BY review_date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_review(self, review_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_date=?", (review_date,)
            ).fetchone()
        return dict(row) if row else None

    def save_summary_report(
        self,
        kind: str,
        period_key: str,
        report_date: str,
        period_start: str,
        period_end: str,
        summary: str,
        content_path: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO summary_reports(
                    kind, period_key, report_date, period_start, period_end,
                    created_at, summary, content_path
                )
                VALUES(?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)
                ON CONFLICT(kind, period_key) DO UPDATE SET
                    report_date=excluded.report_date,
                    period_start=excluded.period_start,
                    period_end=excluded.period_end,
                    created_at=datetime('now', 'localtime'),
                    summary=excluded.summary,
                    content_path=excluded.content_path
                """,
                (
                    kind,
                    period_key,
                    report_date,
                    period_start,
                    period_end,
                    summary,
                    content_path,
                ),
            )

    def get_summary_report(self, kind: str, period_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM summary_reports WHERE kind=? AND period_key=?",
                (kind, period_key),
            ).fetchone()
        return dict(row) if row else None

    def get_last_summary_report(self, kind: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM summary_reports WHERE kind=? ORDER BY report_date DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    def export_csv(self, path: Path, run_date: str) -> int:
        rows = self.get_snapshots(run_date)
        if not rows:
            return 0
        columns = [
            "code",
            "name",
            "industry",
            "price",
            "daily_pct",
            "ret_20d",
            "ret_250d",
            "dd_1y",
            "vol_1y",
            "pe",
            "pb",
            "market_cap_yi",
            "quality_score",
            "trend_score",
            "position_score",
            "risk_score",
            "score",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
