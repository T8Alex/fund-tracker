from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from shared.formula import FORMULA_VERSION
from shared.trading_calendar import count_distinct_dates_since

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
    formula_version TEXT,
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
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT NOT NULL,
    run_date TEXT NOT NULL,
    fund_code TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 1,
    errors TEXT,
    PRIMARY KEY (source, run_date, fund_code)
);
CREATE TABLE IF NOT EXISTS run_artifacts (
    run_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_date, kind)
);
"""


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row for row in rows}


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> bool:
    """Add a column when missing. Returns True when the column was added."""
    if column in _columns(connection, table):
        return False
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def _migrate_source_health(connection: sqlite3.Connection) -> None:
    """Rebuild source_health so `fund_code` is NOT NULL.

    SQLite treats NULLs as distinct inside a PRIMARY KEY, so the old
    `fund_code TEXT` + `ON CONFLICT(source, run_date, fund_code)` never
    conflicted for market/macro rows: every re-run appended another row and
    the report's "数据完整性" table counted one success per run.
    """
    info = _columns(connection, "source_health")
    if not info:
        return
    fund_code = info.get("fund_code")
    if fund_code is not None and fund_code["notnull"]:
        return
    connection.execute(
        """
        CREATE TABLE source_health_rebuild (
            source TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fund_code TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 1,
            errors TEXT,
            PRIMARY KEY (source, run_date, fund_code)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO source_health_rebuild(source, run_date, fund_code, success, errors)
        SELECT source,
               run_date,
               COALESCE(fund_code, '') AS fc,
               MAX(success),
               MAX(errors)
        FROM source_health
        GROUP BY source, run_date, fc
        """
    )
    connection.execute("DROP TABLE source_health")
    connection.execute("ALTER TABLE source_health_rebuild RENAME TO source_health")



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
            _migrate_source_health(connection)
            added = _ensure_column(
                connection,
                "snapshots",
                "formula_version",
                "formula_version TEXT",
            )
            if added:
                # Existing snapshots were produced by the current formula;
                # backfill so same-version comparisons keep working.
                connection.execute(
                    "UPDATE snapshots SET formula_version=? WHERE formula_version IS NULL",
                    (FORMULA_VERSION,),
                )
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

    def save_snapshot(
        self,
        code: str,
        run_date: str,
        metrics: dict,
        formula_version: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(code, run_date, metrics, score, formula_version)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(code, run_date) DO UPDATE SET
                    metrics=excluded.metrics,
                    score=excluded.score,
                    formula_version=excluded.formula_version
                """,
                (
                    code,
                    run_date,
                    json.dumps(metrics, ensure_ascii=False),
                    metrics.get("score"),
                    formula_version or metrics.get("formula_version") or FORMULA_VERSION,
                ),
            )

    def get_snapshot(self, code: str, run_date: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metrics FROM snapshots WHERE code=? AND run_date=?",
                (code, run_date),
            ).fetchone()
        return json.loads(row["metrics"]) if row else None

    def get_previous_snapshot(
        self,
        code: str,
        before_date: str,
        formula_version: str | None = None,
    ) -> dict | None:
        """Latest earlier snapshot, optionally restricted to one formula version.

        Comparing scores produced by different formulas would fake a
        "score dropped N points" alert, so callers pass the current version.
        """
        query = """
            SELECT metrics FROM snapshots
            WHERE code=? AND run_date < ?{version_clause}
            ORDER BY run_date DESC LIMIT 1
        """
        params: list = [code, before_date]
        version_clause = ""
        if formula_version:
            version_clause = " AND COALESCE(formula_version, ?) = ?"
            params.extend([formula_version, formula_version])
        with self._connect() as connection:
            row = connection.execute(
                query.format(version_clause=version_clause), params
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

    def save_artifact(self, run_date: str, kind: str, payload) -> None:
        """Persist a run-scoped JSON payload (market / macro snapshot).

        Reports must be reproducible: rebuilding a past report has to reuse the
        market and macro data seen on that day, not today's live values.
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_artifacts(run_date, kind, payload, created_at)
                VALUES(?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(run_date, kind) DO UPDATE SET
                    payload=excluded.payload,
                    created_at=excluded.created_at
                """,
                (run_date, kind, json.dumps(payload, ensure_ascii=False)),
            )

    def get_artifact(self, run_date: str, kind: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM run_artifacts WHERE run_date=? AND kind=?",
                (run_date, kind),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

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

    def save_source_health(self, source: str, run_date: str, fund_code: str | None, success: bool, errors: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_health(source, run_date, fund_code, success, errors)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source, run_date, fund_code) DO UPDATE SET
                    success=excluded.success,
                    errors=excluded.errors
                """,
                (source, run_date, fund_code or "", 1 if success else 0, errors),
            )

    def get_source_health(self, run_date: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source, fund_code, success, errors, COUNT(*) OVER (PARTITION BY source) AS total"
                " FROM source_health WHERE run_date=? ORDER BY source, fund_code",
                (run_date,),
            ).fetchall()
        return [dict(r) for r in rows]

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
            return count_distinct_dates_since(connection, "nav", start_date, end_date)

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
