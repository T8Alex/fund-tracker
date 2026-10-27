from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .learning import ensure_learning_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    role TEXT,
    target_weight REAL,
    note TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    unit_nav REAL,
    acc_nav REAL,
    daily_pct REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_nav_code_date ON nav(code, date);
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

    def upsert_fund(
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
                INSERT INTO funds(code, name, category, role, target_weight, note, added_at)
                VALUES(?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    category=COALESCE(excluded.category, funds.category),
                    role=COALESCE(excluded.role, funds.role),
                    target_weight=COALESCE(excluded.target_weight, funds.target_weight),
                    note=COALESCE(excluded.note, funds.note)
                """,
                (code, name, category, role, target_weight, note),
            )

    def list_funds(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM funds ORDER BY category, code"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_fund(self, code: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM funds WHERE code=?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def remove_fund(self, code: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM funds WHERE code=?", (code,))
        return cursor.rowcount > 0

    def save_nav(self, code: str, net_series: list, ac_series: list) -> None:
        net_map = {day.isoformat(): value for day, value, _ in net_series}
        daily_map = {
            day.isoformat(): daily for day, _, daily in net_series if daily is not None
        }
        ac_map = {day.isoformat(): value for day, value in ac_series}
        dates = sorted(set(net_map) | set(ac_map))
        rows = [
            (code, day, net_map.get(day), ac_map.get(day), daily_map.get(day))
            for day in dates
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO nav(code, date, unit_nav, acc_nav, daily_pct)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(code, date) DO UPDATE SET
                    unit_nav=excluded.unit_nav,
                    acc_nav=excluded.acc_nav,
                    daily_pct=excluded.daily_pct
                """,
                rows,
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
                    code,
                    run_date,
                    json.dumps(metrics, ensure_ascii=False),
                    metrics.get("score"),
                ),
            )

    def get_snapshot(self, code: str, run_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metrics FROM snapshots WHERE code=? AND run_date=?",
                (code, run_date),
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
                (code, before_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

    def get_snapshots(self, run_date: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT metrics FROM snapshots WHERE run_date=? ORDER BY code",
                (run_date,),
            ).fetchall()
        return [json.loads(row["metrics"]) for row in rows]

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

    def get_nav_window(self, code: str, end_date: str, count: int = 6) -> list[dict]:
        """Return the last `count` NAV rows up to end_date, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT date, unit_nav, acc_nav FROM nav
                WHERE code=? AND date<=?
                ORDER BY date DESC LIMIT ?
                """,
                (code, end_date, count),
            ).fetchall()
        result = []
        for row in reversed(rows):
            nav = row["acc_nav"] if row["acc_nav"] is not None else row["unit_nav"]
            if nav is None:
                continue
            result.append({"date": row["date"], "nav": float(nav)})
        return result

    def get_nav_on_or_before(self, code: str, end_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT date, unit_nav, acc_nav FROM nav
                WHERE code=? AND date<=?
                ORDER BY date DESC LIMIT 1
                """,
                (code, end_date),
            ).fetchone()
        if not row:
            return None
        nav = row["acc_nav"] if row["acc_nav"] is not None else row["unit_nav"]
        if nav is None:
            return None
        return {"date": row["date"], "nav": float(nav)}

    def get_nav_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        """Return NAV rows in [start_date, end_date], oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT date, unit_nav, acc_nav FROM nav
                WHERE code=? AND date>=? AND date<=?
                ORDER BY date
                """,
                (code, start_date, end_date),
            ).fetchall()
        result = []
        for row in rows:
            nav = row["acc_nav"] if row["acc_nav"] is not None else row["unit_nav"]
            if nav is None:
                continue
            result.append({"date": row["date"], "nav": float(nav)})
        return result

    def count_trading_days_since(self, start_date: str, end_date: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT date) AS days FROM nav
                WHERE date > ? AND date <= ?
                """,
                (start_date, end_date),
            ).fetchone()
        return int(row["days"]) if row else 0

    def get_snapshot_on_or_before(self, code: str, end_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT metrics FROM snapshots
                WHERE code=? AND run_date<=?
                ORDER BY run_date DESC LIMIT 1
                """,
                (code, end_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

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
            "category",
            "nav",
            "nav_date",
            "daily_pct",
            "ret_1m",
            "ret_1y",
            "ann_3y",
            "dd_1y",
            "dd_3y",
            "sharpe_3y",
            "rank_pct",
            "scale_yi",
            "manager_name",
            "manager_years",
            "annual_fee_pct",
            "score",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
