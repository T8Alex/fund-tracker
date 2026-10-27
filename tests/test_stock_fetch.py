from __future__ import annotations

import unittest

from stock_tracker.fetch import (
    _parse_eastmoney_kline,
    _parse_eastmoney_quote,
    _stock_symbol,
    is_stock_code,
    stock_exchange_prefix,
    stock_secid,
)


class StockFetchParsingTest(unittest.TestCase):
    def test_stock_code_and_market_helpers(self) -> None:
        self.assertTrue(is_stock_code("600519"))
        self.assertTrue(is_stock_code("000001"))
        self.assertFalse(is_stock_code("60051"))
        self.assertEqual(stock_secid("600519"), "1.600519")
        self.assertEqual(stock_secid("000001"), "0.000001")
        self.assertEqual(stock_exchange_prefix("600519"), "SH")
        self.assertEqual(stock_exchange_prefix("000001"), "SZ")
        self.assertEqual(stock_exchange_prefix("830799"), "BJ")
        self.assertEqual(_stock_symbol("600519"), "sh600519")
        self.assertEqual(_stock_symbol("000001"), "sz000001")
        self.assertEqual(_stock_symbol("830799"), "bj830799")

    def test_parse_eastmoney_quote_scales_fields(self) -> None:
        text = (
            '{"data":{"f43":1234,"f58":"测试股票","f170":125,"f168":88,'
            '"f162":1500,"f167":150,"f116":10000000000,"f127":"银行"}}'
        )
        quote = _parse_eastmoney_quote("600000", text)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote["name"], "测试股票")
        self.assertAlmostEqual(quote["price"], 12.34)
        self.assertAlmostEqual(quote["pct"], 1.25)
        self.assertAlmostEqual(quote["turnover"], 0.88)
        self.assertAlmostEqual(quote["pe"], 15.0)
        self.assertAlmostEqual(quote["pb"], 1.5)
        self.assertEqual(quote["industry"], "银行")
        self.assertEqual(quote["source"], "eastmoney")

    def test_parse_eastmoney_kline_fields(self) -> None:
        text = (
            '{"data":{"klines":["2026-10-22,10.0,10.2,10.4,9.9,1000,100000,5.0,2.0,0.2,1.1",'
            '"2026-10-23,10.2,10.1,10.5,10.0,1200,120000,4.9,-1.0,-0.1,1.2"]}}'
        )
        rows = _parse_eastmoney_kline(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-10-22")
        self.assertAlmostEqual(rows[0]["close"], 10.2)
        self.assertAlmostEqual(rows[0]["pct"], 2.0)
        self.assertAlmostEqual(rows[1]["turnover"], 1.2)


if __name__ == "__main__":
    unittest.main()
