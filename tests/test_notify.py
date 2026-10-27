from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from fund_tracker.notify import (
    feishu_card_notify,
    feishu_notify,
    feishu_sign,
    generic_webhook_notify,
    is_feishu_webhook,
    notify_report,
    report_to_mobile_markdown,
    webhook_notify,
)


class _Response:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {"code": 0, "msg": "success"}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class NotifyTest(unittest.TestCase):
    def test_feishu_url_detection(self) -> None:
        self.assertTrue(
            is_feishu_webhook(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
            )
        )
        self.assertTrue(
            is_feishu_webhook(
                "https://open.larksuite.com/open-apis/bot/v2/hook/abc"
            )
        )
        self.assertFalse(is_feishu_webhook("https://example.com/hook/abc"))

    def test_feishu_sign_is_base64(self) -> None:
        value = feishu_sign("secret", "1599360473")
        self.assertEqual(value, base64.b64encode(base64.b64decode(value)).decode())
        self.assertNotEqual(value, feishu_sign("secret2", "1599360473"))
        self.assertNotEqual(value, feishu_sign("secret", "1599360474"))

    def test_feishu_text_payload_with_signature(self) -> None:
        with patch(
            "fund_tracker.notify.requests.post",
            return_value=_Response(),
        ) as post:
            ok = feishu_notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "基金2日推荐",
                "组合 2 日 -0.02%",
                secret="secret",
            )
        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("基金2日推荐", payload["content"]["text"])
        self.assertIn("sign", payload)
        self.assertIn("timestamp", payload)

    def test_feishu_text_payload_without_signature(self) -> None:
        with patch(
            "fund_tracker.notify.requests.post",
            return_value=_Response(),
        ) as post:
            ok = feishu_notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "基金跟踪日报",
                "正文",
            )
        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("sign", payload)
        self.assertNotIn("timestamp", payload)

    def test_feishu_error_code(self) -> None:
        with patch(
            "fund_tracker.notify.requests.post",
            return_value=_Response({"code": 19021, "msg": "sign fail"}),
        ):
            ok = feishu_notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "标题",
                "正文",
            )
        self.assertFalse(ok)

    def test_generic_webhook_payload(self) -> None:
        with patch(
            "fund_tracker.notify.requests.post",
            return_value=_Response(),
        ) as post:
            ok = generic_webhook_notify(
                "https://example.com/hook",
                "标题",
                "正文",
            )
        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["body"], "正文")

    def test_webhook_routes_feishu(self) -> None:
        with patch(
            "fund_tracker.notify.feishu_notify",
            return_value=True,
        ) as feishu:
            ok = webhook_notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "标题",
                "正文",
                secret="secret",
            )
        self.assertTrue(ok)
        feishu.assert_called_once()

    def test_feishu_card_payload(self) -> None:
        with patch(
            "fund_tracker.notify.requests.post",
            return_value=_Response(),
        ) as post:
            ok = feishu_card_notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "基金2日推荐",
                "# 标题\n\n正文",
                secret="secret",
            )
        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["msg_type"], "interactive")
        card = payload["card"]
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["title"]["content"], "基金2日推荐")
        self.assertIn("正文", card["body"]["elements"][0]["content"])
        self.assertIn("sign", payload)

    def test_notify_report_sends_card(self) -> None:
        config = {
            "notify": {
                "macos": False,
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "webhook_secret": "secret",
                "full_report": "review",
            }
        }
        with patch(
            "fund_tracker.notify.feishu_card_notify",
            return_value=True,
        ) as card:
            ok = notify_report(config, "标题", "摘要", "完整报告")
        self.assertTrue(ok)
        card.assert_called_once()

    def test_report_to_mobile_markdown_converts_tables(self) -> None:
        markdown = (
            "# 标题\n\n"
            "| 指数 | 最新 | 日涨跌 |\n"
            "|---|---:|---:|\n"
            "| 沪深300 | 4105.33 | +1.71% |\n"
            "| 红利指数 | 3209.40 | -0.60% |\n"
        )
        mobile = report_to_mobile_markdown(markdown)
        self.assertNotIn("| 指数 |", mobile)
        self.assertIn("- **沪深300**", mobile)
        self.assertIn("最新：4105.33", mobile)
        self.assertIn("日涨跌：+1.71%", mobile)

    def test_report_to_mobile_markdown_splits_principles_card(self) -> None:
        markdown = (
            "| 标的 | 行动：做什么 | 坚持：逻辑 / 证伪 | 眼光：质量 / 估值 / 位置 |\n"
            "|---|---|---|---|\n"
            "| 测试银行（600000） | 观察；10–12 | 零售银行；证伪：评分<55 | 质量80、PE10 |\n"
        )
        mobile = report_to_mobile_markdown(markdown)
        self.assertIn("- **测试银行（600000）**", mobile)
        self.assertIn("  - 行动：做什么：观察；10–12", mobile)
        self.assertIn("  - 坚持：逻辑 / 证伪：零售银行；证伪：评分<55", mobile)
        self.assertIn("  - 眼光：质量 / 估值 / 位置：质量80、PE10", mobile)

    def test_report_to_mobile_markdown_keeps_empty_learning_metrics(self) -> None:
        markdown = (
            "| 周期 | 已评估 | 方向命中 | 未中 | 走平 | 观察 | 待评估 | 方向命中率 | 平均涨跌 | 平均最大不利 |\n"
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
            "| 2 日 | 0 | 0 | 0 | 0 | 0 | 8 | — | — | — |\n"
        )
        mobile = report_to_mobile_markdown(markdown)
        self.assertIn("方向命中率：—", mobile)
        self.assertIn("平均最大不利：—", mobile)

    def test_notify_report_falls_back_to_text(self) -> None:
        config = {
            "notify": {
                "macos": False,
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                "webhook_secret": "secret",
            }
        }
        with patch(
            "fund_tracker.notify.feishu_card_notify",
            return_value=False,
        ), patch(
            "fund_tracker.notify.feishu_notify",
            return_value=True,
        ) as text:
            ok = notify_report(config, "标题", "摘要", "完整报告")
        self.assertTrue(ok)
        text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
