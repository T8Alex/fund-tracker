from __future__ import annotations

import unittest

from scripts.verify_daily import _integrity_counts


class IntegrityCountsTest(unittest.TestCase):
    def test_healthy_row(self) -> None:
        row = "| 大盘指数 | 1 | 0 | ✅ 正常 |"
        self.assertEqual(_integrity_counts(row), (1, 0))

    def test_failed_row_is_not_healthy(self) -> None:
        # 回归：旧实现用 "| 1 |" in row 判断，这一行会被误判为通过
        row = "| 大盘指数 | 0 | 1 | ❌ 失败 |"
        self.assertEqual(_integrity_counts(row), (0, 1))
        self.assertNotEqual(_integrity_counts(row), (1, 0))

    def test_duplicated_rows_are_not_healthy(self) -> None:
        row = "| 大盘指数 | 9 | 0 | ⚠️ 重复 |"
        self.assertEqual(_integrity_counts(row), (9, 0))

    def test_malformed_row_returns_none(self) -> None:
        self.assertIsNone(_integrity_counts(""))
        self.assertIsNone(_integrity_counts("| 大盘指数 | 正常 |"))
        self.assertIsNone(_integrity_counts("| 大盘指数 | 一 | 零 | ✅ |"))


if __name__ == "__main__":
    unittest.main()
