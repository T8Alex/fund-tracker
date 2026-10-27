from __future__ import annotations

import unittest

from fund_tracker.cli import _select_announcements
from fund_tracker.fetch import _parse_holding_table


STOCK_HTML = """
var apidata={ content:"<div class='box'><div class='boxitem w790'><h4 class='t'><label class='left'><a>测试基金</a>&nbsp;&nbsp;2026年2季度股票投资明细</label><label class='right lab2 xq505'>&nbsp;&nbsp;&nbsp;&nbsp;来源：天天基金&nbsp;&nbsp;&nbsp;&nbsp;截止至：<font class='px12'>2026-06-30</font></label></h4><table class='w782 comm tzxq'><thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>最新价</th><th>涨跌幅</th><th>相关资讯</th><th>占净值比例</th><th>持股数</th><th>持仓市值</th></tr></thead><tbody><tr><td>1</td><td>300308</td><td class='tol'>中际旭创</td><td></td><td></td><td></td><td class='tor'>1.56%</td><td class='tor'>25.07</td><td class='tor'>31,838.90</td></tr></tbody></table></div></div>",arryear:[2026],curyear:2026};
"""

BOND_HTML = """
var apidata={ content:"<div class='box'><div class='boxitem w790'><h4 class='t'><label class='left'><a>测试基金</a>&nbsp;&nbsp;2026年2季度债券投资明细</label><label class='right lab2 xq505'>&nbsp;&nbsp;&nbsp;&nbsp;来源：天天基金&nbsp;&nbsp;&nbsp;&nbsp;截止至：<font class='px12'>2026-06-30</font></label></h4><table class='w782 comm tzxq'><thead><tr><th>序号</th><th>债券代码</th><th>债券名称</th><th>占净值比例</th><th>持仓市值</th></tr></thead><tbody><tr><td>1</td><td>250211</td><td class='tol'>25国开11</td><td class='tor'>6.73%</td><td class='tor'>6,077.19</td></tr></tbody></table></div></div>",arryear:[2026],curyear:2026};
"""


class HoldingsTest(unittest.TestCase):
    def test_parse_stock_holding(self) -> None:
        parsed = _parse_holding_table(STOCK_HTML, "stock")
        self.assertEqual(parsed["report_date"], "2026-06-30")
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["code"], "300308")
        self.assertEqual(parsed["items"][0]["name"], "中际旭创")
        self.assertAlmostEqual(parsed["items"][0]["weight_pct"], 1.56)

    def test_parse_bond_holding(self) -> None:
        parsed = _parse_holding_table(BOND_HTML, "bond")
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["name"], "25国开11")
        self.assertAlmostEqual(parsed["items"][0]["weight_pct"], 6.73)

    def test_select_announcements_keeps_annual_report(self) -> None:
        items = [{"title": f"日常公告 {i}", "date": "2026-10-01"} for i in range(15)]
        items.append(
            {
                "title": "测试基金2025年年度报告",
                "date": "2026-03-31",
                "url": "https://example.com/annual.pdf",
            }
        )
        selected = _select_announcements(items)
        titles = [item["title"] for item in selected]
        self.assertIn("测试基金2025年年度报告", titles)


if __name__ == "__main__":
    unittest.main()
