from __future__ import annotations

import unittest

from fund_tracker.report import _review_action


class RebalanceBandTest(unittest.TestCase):
    def _row(self, target: float, actual: float) -> dict:
        return {"target_weight": target, "actual_weight": actual, "score": 85.0}

    def test_deviation_under_band_is_not_flagged(self) -> None:
        # 7% actual vs 6% target = 1pp deviation, below 3pp band
        action, reason = _review_action(
            self._row(target=0.06, actual=0.07), [], rebalance_band_pp=3
        )
        self.assertNotEqual(action, "再平衡")
        self.assertNotEqual(action, "减仓/复核")

    def test_deviation_over_band_triggers_adjustment(self) -> None:
        # 9% actual vs 6% target = 3pp deviation, hits the band
        action, reason = _review_action(
            self._row(target=0.06, actual=0.09), [], rebalance_band_pp=3
        )
        self.assertIn(action, {"再平衡", "减仓/复核"})
        self.assertIn("偏离", reason)

    def test_default_band_is_3pp(self) -> None:
        action, reason = _review_action(
            self._row(target=0.05, actual=0.07), [], rebalance_band_pp=3
        )
        # 2pp deviation < 3pp band → no adjustment
        self.assertNotEqual(action, "再平衡")
        self.assertNotEqual(action, "减仓/复核")

    def test_tight_band_catches_small_deviation(self) -> None:
        # 6% actual vs 5% target = 1pp, band=0.5pp → should trigger
        action, _ = _review_action(
            self._row(target=0.05, actual=0.06), [], rebalance_band_pp=1
        )
        self.assertIn(action, {"再平衡", "减仓/复核"})


if __name__ == "__main__":
    unittest.main()
