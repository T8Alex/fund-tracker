"""Tests for the watchdog's pure decision logic evaluate_watch()."""

from __future__ import annotations

import unittest
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from scripts.watchdog import CUTOFFS, evaluate_watch
from shared.trading_calendar import is_last_weekday

TZ = ZoneInfo("Asia/Shanghai")


def _dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def ok_facts(day: int = 28) -> dict:
    return {
        "stock": {"has_run_today": True, "notify_ok": True},
        "fund": {"has_run_today": True, "notify_ok": True},
        "model": {"run_ok": True, "skipped": False},
        "backup": {
            "latest_ts": datetime(2026, 10, day, 18, 45, tzinfo=TZ).isoformat(
                timespec="seconds"
            )
        },
        "data": {"latest": f"2026-10-{day:02d}", "stale_trading_days": 0},
    }


class IsLastWeekdayTest(unittest.TestCase):
    def test_friday_oct_30_2026(self) -> None:
        # 2026-10-31 is a Saturday, so the last weekday is Friday 2026-10-30.
        self.assertTrue(is_last_weekday(_dt(30).date()))
        self.assertFalse(is_last_weekday(_dt(29).date()))
        self.assertFalse(is_last_weekday(_dt(31).date()))

    def test_nov_30_2026_is_monday_last_weekday(self) -> None:
        self.assertTrue(is_last_weekday(datetime(2026, 11, 30, tzinfo=TZ).date()))

    def test_december_wraps_year(self) -> None:
        # 2026-12-31 is a Thursday → last weekday of December 2026.
        self.assertTrue(is_last_weekday(datetime(2026, 12, 31, tzinfo=TZ).date()))


class WatchTest(unittest.TestCase):
    def test_weekend_never_alerts(self) -> None:
        saturday = datetime(2026, 10, 31, 20, 0, tzinfo=TZ)
        facts = {"stock": {}, "fund": {}, "model": {}, "backup": {}, "data": {"stale_days": 9}}
        self.assertEqual(evaluate_watch(saturday, facts), [])

    def test_before_cutoff_no_alerts(self) -> None:
        # 16:55 is before the stock cutoff; nothing has run yet — no alerts.
        self.assertEqual(evaluate_watch(_dt(28, 16, 55), ok_facts()), [])
        self.assertEqual(evaluate_watch(_dt(28, 16, 55), {"stock": {}, "fund": {}}), [])

    def test_missing_stock_run_after_cutoff(self) -> None:
        facts = ok_facts()
        facts["stock"] = {"has_run_today": False}
        alerts = evaluate_watch(_dt(28, 17, 15), facts)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["job"], "stock")
        self.assertEqual(alerts[0]["severity"], "high")

    def test_missing_fund_run_after_cutoff(self) -> None:
        facts = ok_facts()
        facts["fund"] = {"has_run_today": False}
        alerts = evaluate_watch(_dt(28, 21, 15), facts)
        self.assertEqual([a["job"] for a in alerts], ["fund"])

    def test_push_failure_alerts_only_when_webhook_used(self) -> None:
        facts = ok_facts()
        facts["fund"] = {"has_run_today": True, "notify_ok": False}
        alerts = evaluate_watch(_dt(28, 21, 15), facts)
        self.assertEqual([a["job"] for a in alerts], ["fund"])
        # notify_ok=None (e.g. webhook not configured) must NOT alert.
        facts["fund"] = {"has_run_today": True, "notify_ok": None}
        self.assertEqual(evaluate_watch(_dt(28, 21, 15), facts), [])

    def test_all_ok_no_alerts(self) -> None:
        self.assertEqual(evaluate_watch(_dt(28, 21, 15), ok_facts()), [])

    def test_model_not_last_weekday_alerts_without_skip_record(self) -> None:
        facts = ok_facts()
        facts["model"] = {"run_ok": False, "skipped": False}
        alerts = evaluate_watch(_dt(28, 20, 51), facts)
        self.assertEqual([a["job"] for a in alerts], ["model"])
        self.assertEqual(alerts[0]["severity"], "medium")

    def test_model_skip_record_suppresses_alert(self) -> None:
        facts = ok_facts()
        facts["model"] = {"run_ok": False, "skipped": True}
        self.assertEqual(evaluate_watch(_dt(28, 20, 51), facts), [])

    def test_model_last_weekday_missing_report_is_high(self) -> None:
        # 2026-10-30 is the last weekday of October 2026.
        facts = ok_facts(day=30)
        facts["model"] = {"run_ok": False, "skipped": True}
        alerts = evaluate_watch(_dt(30, 20, 51), facts)
        self.assertEqual([a["job"] for a in alerts], ["model"])
        self.assertEqual(alerts[0]["severity"], "high")

    def test_backup_missing_archive_after_2020(self) -> None:
        facts = ok_facts()
        facts["backup"] = {"latest_ts": None}
        alerts = evaluate_watch(_dt(28, 20, 25), facts)
        self.assertEqual([a["job"] for a in alerts], ["backup"])

    def test_backup_fresh_archive_no_alert(self) -> None:
        facts = ok_facts()  # latest 18:45 same day → fresher than 30h
        self.assertEqual(evaluate_watch(_dt(28, 20, 25), facts), [])

    def test_backup_stale_archive_alerts(self) -> None:
        facts = ok_facts()
        stale = datetime(2026, 10, 27, 8, 0, tzinfo=TZ)  # >30h before 20:25 on 10-28
        facts["backup"] = {"latest_ts": stale.isoformat(timespec="seconds")}
        alerts = evaluate_watch(_dt(28, 20, 25), facts)
        self.assertEqual([a["job"] for a in alerts], ["backup"])

    def test_backup_check_silent_before_1815(self) -> None:
        facts = ok_facts()
        facts["backup"] = {"latest_ts": None}
        self.assertEqual(evaluate_watch(_dt(28, 18, 30), facts), [])

    def test_stale_data_alerts_after_three_days(self) -> None:
        facts = ok_facts()
        facts["data"] = {"latest": "2026-10-24", "stale_trading_days": 4}
        alerts = evaluate_watch(_dt(28, 20, 15), facts)
        self.assertEqual([a["job"] for a in alerts], ["data"])
        self.assertEqual(alerts[0]["severity"], "medium")

    def test_unknown_staleness_is_silent(self) -> None:
        facts = ok_facts()
        facts["data"] = {"latest": None, "stale_trading_days": None}
        self.assertEqual(evaluate_watch(_dt(28, 20, 15), facts), [])

    def test_missing_run_on_market_holiday_weekday_is_silent(self) -> None:
        # 2026-02-16（周一）是春节休市日：任务没跑不算故障
        holiday = datetime(2026, 2, 16, 17, 15, tzinfo=TZ)
        facts = ok_facts(day=28)
        facts["stock"] = {"has_run_today": False}
        facts["fund"] = {"has_run_today": False}
        self.assertEqual(evaluate_watch(holiday, facts), [])

    def test_missing_run_on_trading_day_still_alerts_after_cutover(self) -> None:
        # 同样配置放到正常交易日（10-28 周三）应报警，保证上面不是假阴性
        facts = ok_facts(day=28)
        facts["stock"] = {"has_run_today": False}
        alerts = evaluate_watch(_dt(28, 17, 15), facts)
        self.assertEqual([a["job"] for a in alerts], ["stock"])

    def test_custom_cutoffs_respected(self) -> None:
        facts = ok_facts()
        facts["stock"] = {"has_run_today": False}
        tight = dict(CUTOFFS, stock=dt_time(9, 0))
        alerts = evaluate_watch(_dt(28, 9, 5), facts, cutoffs=tight)
        self.assertEqual([a["job"] for a in alerts], ["stock"])

    def test_alerts_are_dicts_with_expected_keys(self) -> None:
        facts = ok_facts()
        facts["stock"] = {}
        alerts = evaluate_watch(_dt(28, 17, 15), facts)
        for alert in alerts:
            self.assertIn("job", alert)
            self.assertIn("severity", alert)
            self.assertIn("message", alert)


if __name__ == "__main__":
    unittest.main()
