from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fund_tracker.ai_digest import (
    LocalModelError,
    _call_local,
    _extract_json,
    _model_loaded,
    _normalise_daily,
    build_daily_digest,
    build_weekly_digest,
    render_ai_section,
    render_weekly_ai_section,
)
from fund_tracker.cli import _ai_daily_section
from fund_tracker.store import Database


def daily_content() -> dict:
    return {
        "summary": "测试摘要：资金面平稳，海外波动加大。",
        "facts": [
            {
                "source_id": 1,
                "fact": "央行开展逆回购。",
                "impact": "资金面保持平稳。",
            },
            {
                "source_id": 0,
                "fact": "中国 10Y 国债收益率 1.75%。",
                "impact": "利率环境偏低。",
            },
        ],
        "bullish": ["国内利率偏低。"],
        "bearish": ["海外利率偏高。"],
        "risks": ["油价上涨。"],
        "watch": ["人民币汇率。"],
        "confidence": 70,
        "limitations": "只有标题和摘要，未读全文。",
    }


def weekly_content() -> dict:
    return {
        "week_summary": "本周组合震荡，宏观变量仍是主要矛盾。",
        "confirmed": ["资金面平稳。"],
        "challenged": ["海外波动短期影响。"],
        "next_week_watch": ["油价和汇率。"],
        "risks": ["海外利率上行。"],
        "confidence": 60,
        "limitations": "每日摘要天数有限。",
    }


def ai_config() -> dict:
    return {
        "enabled": True,
        "max_news": 8,
        "intro_chars": 200,
        "max_input_chars": 12000,
        "release_after_call": False,
        "daily": {
            "endpoint": "http://127.0.0.1:11436/v1/chat/completions",
            "model": "qwen3.8:27b",
            "timeout_seconds": 10,
            "max_tokens": 1000,
            "temperature": 0.1,
            "enable_thinking": False,
        },
        "weekly": {
            "enabled": True,
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
            "model": "deepseek-r1:32b",
            "timeout_seconds": 20,
            "max_tokens": 1200,
            "temperature": 0.1,
        },
    }


def macro_snapshot() -> dict:
    return {
        "china": {"10y": 1.75, "10y_5d_bp": -3, "2y": 1.4},
        "global": {
            "us_10y": 4.1,
            "us_10y_5d_bp": 8,
            "us_2y": 3.8,
            "usdcny": {"current": 7.12},
            "oil": {"current": 82},
            "gold": {"current": 2400},
        },
        "indices": [
            {
                "name": "沪深300",
                "pct": 0.3,
                "ret_20d": 2.1,
                "trend": "震荡",
            }
        ],
        "news": [
            {
                "source": "测试源",
                "published": "10-25 10:00",
                "title": "央行开展逆回购操作",
                "intro": "市场资金利率小幅回落。",
                "tags": ["利率"],
            },
            {
                "source": "测试源",
                "published": "10-25 11:00",
                "title": "国际油价上涨",
                "intro": "避险情绪有所升温。",
                "tags": ["油价"],
            },
        ],
    }


class AiDigestTest(unittest.TestCase):
    def test_extract_json_handles_fence_and_text(self) -> None:
        text = '说明：\n```json\n{"summary": "ok"}\n```\n'
        self.assertEqual(_extract_json(text), {"summary": "ok"})

    def test_extract_json_rejects_invalid(self) -> None:
        with self.assertRaises(LocalModelError):
            _extract_json("不是 JSON")

    def test_normalise_daily_clears_non_news_source_id(self) -> None:
        value = _normalise_daily(daily_content())
        self.assertEqual(value["facts"][0]["source_id"], "1")
        self.assertEqual(value["facts"][1]["source_id"], "")
        self.assertEqual(value["confidence"], 70)

    def test_render_has_disclaimer_and_sources(self) -> None:
        result = {
            "content": _normalise_daily(daily_content()),
            "model": "qwen3.8:27b",
            "cached": False,
            "news": macro_snapshot()["news"],
        }
        text = render_ai_section(result)
        self.assertIn("AI 新闻与文章摘要（本地模型）", text)
        self.assertIn("不构成投资建议", text)
        self.assertIn("本次输入来源", text)
        self.assertIn("央行开展逆回购操作", text)

    def test_daily_disabled_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "test.db"
            config = ai_config()
            config["enabled"] = False
            with patch("fund_tracker.ai_digest._call_local") as call:
                result = build_daily_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-25",
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=config,
                )
        self.assertIsNone(result)
        call.assert_not_called()

    def test_daily_cached_second_call_does_not_call_model_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "test.db"
            with patch(
                "fund_tracker.ai_digest._call_local",
                return_value=json.dumps(daily_content(), ensure_ascii=False),
            ) as call:
                first = build_daily_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-25",
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
                second = build_daily_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-25",
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(call.call_count, 1)
        self.assertIn("本次复用", render_ai_section(second))

    def test_daily_drops_unknown_source_id(self) -> None:
        content = daily_content()
        content["facts"][0]["source_id"] = 99
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "test.db"
            with patch(
                "fund_tracker.ai_digest._call_local",
                return_value=json.dumps(content, ensure_ascii=False),
            ):
                result = build_daily_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-25",
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
        self.assertEqual(result["content"]["facts"][0]["source_id"], "")

    def test_weekly_builds_from_current_news_when_daily_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "test.db"
            with patch(
                "fund_tracker.ai_digest._call_local",
                return_value=json.dumps(weekly_content(), ensure_ascii=False),
            ):
                result = build_weekly_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-23",
                    period_start="2026-10-16",
                    period_end="2026-10-23",
                    period_label="本周",
                    rows=[{"code": "000001", "name": "测试基金", "review_action": "持有"}],
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
        self.assertIsNotNone(result)
        self.assertEqual(result["source_days"], 0)
        self.assertIn("AI 周度复盘（本地模型）", render_weekly_ai_section(result))

    def test_cli_ai_failure_degrades_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            config = {"ai": {"enabled": True}}
            with patch(
                "fund_tracker.cli.build_daily_digest",
                side_effect=LocalModelError("模型离线"),
            ):
                text = _ai_daily_section(
                    database,
                    config,
                    macro_snapshot(),
                    "2026-10-25",
                )
        self.assertIn("本次未生成", text)
        self.assertIn("模型离线", text)

    def test_cli_ai_disabled_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.init()
            text = _ai_daily_section(
                database,
                {"ai": {"enabled": False}},
                macro_snapshot(),
                "2026-10-25",
            )
        self.assertEqual(text, "")

    def test_weekly_reads_daily_digest_from_same_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "test.db"
            with patch(
                "fund_tracker.ai_digest._call_local",
                side_effect=[
                    json.dumps(daily_content(), ensure_ascii=False),
                    json.dumps(weekly_content(), ensure_ascii=False),
                ],
            ) as call:
                build_daily_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-23",
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
                result = build_weekly_digest(
                    db_path=db,
                    module="基金",
                    digest_date="2026-10-23",
                    period_start="2026-10-16",
                    period_end="2026-10-23",
                    period_label="本周",
                    rows=[{"code": "000001", "name": "测试基金", "review_action": "持有"}],
                    news=macro_snapshot()["news"],
                    macro_snapshot=macro_snapshot(),
                    config=ai_config(),
                )
        self.assertEqual(result["source_days"], 1)
        self.assertEqual(call.call_count, 2)

    def test_external_endpoint_is_rejected(self) -> None:
        with self.assertRaises(LocalModelError):
            _call_local(
                "https://api.example.com/v1/chat/completions",
                {"model": "x", "messages": []},
                10,
                release=False,
            )

    def test_model_loaded_matches_requested_model(self) -> None:
        endpoint = "http://127.0.0.1:11434/v1/chat/completions"
        with patch(
            "fund_tracker.ai_digest._get_json",
            return_value={"models": [{"name": "deepseek-r1:32b"}]},
        ):
            self.assertTrue(_model_loaded(endpoint, "deepseek-r1:32b"))
            self.assertFalse(_model_loaded(endpoint, "qwen3.8:27b"))

    def test_call_local_unloads_only_when_cold(self) -> None:
        payload = {"model": "qwen3.8:27b", "messages": []}
        response = {
            "choices": [{"message": {"content": '{"ok": true}'}}]
        }
        with patch(
            "fund_tracker.ai_digest._model_loaded", return_value=False
        ), patch(
            "fund_tracker.ai_digest._post_json", return_value=response
        ), patch("fund_tracker.ai_digest._unload_model") as unload:
            text = _call_local(
                "http://127.0.0.1:11436/v1/chat/completions",
                payload,
                10,
                release=True,
            )
        self.assertEqual(text, '{"ok": true}')
        unload.assert_called_once()

        with patch(
            "fund_tracker.ai_digest._model_loaded", return_value=True
        ), patch(
            "fund_tracker.ai_digest._post_json", return_value=response
        ), patch("fund_tracker.ai_digest._unload_model") as unload:
            _call_local(
                "http://127.0.0.1:11436/v1/chat/completions",
                payload,
                10,
                release=True,
            )
        unload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
