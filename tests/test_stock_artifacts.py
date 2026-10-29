from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared.market import market_close, market_history, market_range
from stock_tracker import cli
from stock_tracker.store import Database


def _bars(closes: list[float], start_day: int = 1) -> list[dict]:
    return [
        {
            "date": f"2026-10-{start_day + index:02d}",
            "open": close,
            "close": close,
            "high": close,
            "low": close,
            "pct": 0.5 if index else 0.0,
        }
        for index, close in enumerate(closes)
    ]


SERIES = {
    "沪深300": _bars([100, 101, 102, 103, 104, 105]),
    "中证500": _bars([200, 198, 196, 194, 192, 190]),
    "创业板指": _bars([300, 300]),
}


class MarketSeriesTest(unittest.TestCase):
    def test_market_history_matches_fetch_shape(self) -> None:
        rows = market_history(SERIES, 3)
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {"沪深300", "中证500"})
        self.assertEqual(by_name["沪深300"]["window_days"], 2)
        self.assertEqual(by_name["沪深300"]["current"], 105)
        # count=3 → 与 3 根中最早那根（10-04，103）相比
        self.assertAlmostEqual(by_name["沪深300"]["pct_window"], (105 / 103 - 1) * 100)
        self.assertEqual(by_name["沪深300"]["date"], "2026-10-06")

    def test_short_series_is_skipped(self) -> None:
        rows = market_history({"创业板指": _bars([300, 300, 300])}, 4)
        self.assertEqual(rows, [])

    def test_as_of_drops_later_bars(self) -> None:
        rows = market_history(SERIES, 2, as_of="2026-10-04")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["沪深300"]["date"], "2026-10-04")
        self.assertEqual(by_name["沪深300"]["current"], 103)
        # 只有 4 根可用 bar，count=2 仍然够
        self.assertAlmostEqual(by_name["沪深300"]["pct_window"], (103 / 102 - 1) * 100)

    def test_market_range_uses_window_ends(self) -> None:
        rows = market_range(SERIES, "2026-10-02", "2026-10-06")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["沪深300"]["start_date"], "2026-10-02")
        self.assertEqual(by_name["沪深300"]["window_days"], 4)
        self.assertAlmostEqual(by_name["沪深300"]["pct_window"], (105 / 101 - 1) * 100)

    def test_market_close_only_uses_exact_day(self) -> None:
        rows = market_close(SERIES, "2026-10-05")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["沪深300"]["current"], 104)
        self.assertEqual(by_name["沪深300"]["date"], "2026-10-05")

    def test_market_close_skips_missing_day(self) -> None:
        # 10-07 没有任何 bar（周末/休市）：宁可空，也不沿用前一天
        self.assertEqual(market_close(SERIES, "2026-10-07"), [])


class ArtifactStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "stock.db")
        self.database.init()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_artifact_roundtrip_and_overwrite(self) -> None:
        self.database.save_artifact("2026-10-29", "market", [{"name": "沪深300"}])
        self.database.save_artifact("2026-10-29", "market", [{"name": "沪深300"}, {"name": "中证500"}])
        rows = self.database.get_artifact("2026-10-29", "market")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(self.database.get_artifact("2026-10-28", "market"))
        self.assertIsNone(self.database.get_artifact("2026-10-29", "macro"))


class ArtifactFirstLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "stock.db")
        self.database.init()
        self.database.save_artifact("2026-10-29", "index_history", SERIES)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_history_prefers_saved_series(self) -> None:
        with mock.patch.object(cli, "fetch_market_history") as fetch:
            rows = cli._market_history_for(self.database, None, "2026-10-29", 3)
        fetch.assert_not_called()
        self.assertEqual(len(rows), 2)

    def test_history_falls_back_when_missing(self) -> None:
        with mock.patch.object(
            cli, "fetch_market_history", return_value=[{"name": "兜底"}]
        ) as fetch:
            rows = cli._market_history_for(self.database, None, "2026-10-01", 3)
        fetch.assert_called_once()
        self.assertEqual(rows, [{"name": "兜底"}])

    def test_range_prefers_saved_series(self) -> None:
        with mock.patch.object(cli, "fetch_market_history_range") as fetch:
            rows = cli._market_range_for(
                self.database, None, "2026-10-29", "2026-10-02", "2026-10-06"
            )
        fetch.assert_not_called()
        self.assertEqual(len(rows), 2)

    def test_macro_prefers_saved_snapshot(self) -> None:
        self.database.save_artifact("2026-10-29", "macro", {"as_of": "2026-10-29", "labels": []})
        with mock.patch.object(cli, "_macro_context") as fetch:
            snapshot, section = cli._resolve_macro(
                self.database, None, {}, "2026-10-29"
            )
        fetch.assert_not_called()
        self.assertEqual(snapshot["as_of"], "2026-10-29")
        self.assertTrue(section)

    def test_macro_falls_back_when_missing(self) -> None:
        with mock.patch.object(
            cli, "_macro_context", return_value=({"as_of": "x"}, "段")
        ) as fetch:
            snapshot, section = cli._resolve_macro(self.database, None, {}, "2026-10-01")
        fetch.assert_called_once()
        self.assertEqual(section, "段")


if __name__ == "__main__":
    unittest.main()
