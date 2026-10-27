from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import subprocess
import time

import requests

LOG = logging.getLogger(__name__)


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_notify(title: str, message: str) -> bool:
    script = (
        f"display notification {_applescript_string(message)} "
        f"with title {_applescript_string(title)}"
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            LOG.warning("macOS 通知失败: %s", stderr or completed.returncode)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("macOS 通知失败: %s", exc)
        return False


def is_feishu_webhook(url: str) -> bool:
    lowered = url.lower()
    return (
        "feishu.cn" in lowered or "larksuite.com" in lowered
    ) and "/hook/" in lowered


def _webhook_secret(notify_config: dict) -> str:
    return (
        notify_config.get("webhook_secret")
        or notify_config.get("feishu_secret")
        or ""
    ).strip()


def feishu_sign(secret: str, timestamp: str) -> str:
    """Feishu custom-bot signature: HMAC-SHA256(empty msg, key=f"{ts}\\n{secret}")."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _feishu_post(url: str, payload: dict, secret: str = "") -> bool:
    if secret:
        timestamp = str(int(time.time()))
        payload = dict(payload)
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return True
        if not isinstance(data, dict):
            return True
        code = data.get("code", data.get("StatusCode"))
        if code is not None and str(code) != "0":
            LOG.warning("飞书机器人返回错误: %s", data)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("飞书通知失败: %s", exc)
        return False


def feishu_notify(url: str, title: str, message: str, secret: str = "") -> bool:
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n{message}"},
    }
    return _feishu_post(url, payload, secret=secret)


def _split_md_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_md_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    for cell in cells:
        value = cell.replace(" ", "")
        if not re.fullmatch(r":?-{3,}:?", value):
            return False
    return True


def _format_table_rows(header: list[str], rows: list[list[str]]) -> list[str]:
    """Turn one Markdown table into mobile-friendly bullet lines.

    Feishu Card 2.0 renders Markdown tables, but a single markdown component
    supports only a few tables and five data rows per table. Bullet lists are
    also much easier to read on a phone, so the Feishu copy uses this format.
    """
    if not rows:
        return []
    headers = [item.strip() for item in header]
    fund_idx = headers.index("基金") if "基金" in headers else None
    code_idx = headers.index("代码") if "代码" in headers else None
    name_idx = headers.index("名称") if "名称" in headers else None

    if name_idx is not None:
        title_idx = name_idx
    elif fund_idx is not None:
        title_idx = fund_idx
    else:
        title_idx = 0

    skip = {title_idx}
    if code_idx is not None:
        skip.add(code_idx)
    if fund_idx is not None:
        skip.add(fund_idx)

    if "公告" in headers and "日期" in headers and fund_idx is not None:
        date_idx = headers.index("日期")
        announcement_idx = headers.index("公告")
        lines: list[str] = []
        for row in rows:
            fund = row[fund_idx] if fund_idx < len(row) else ""
            published = row[date_idx] if date_idx < len(row) else ""
            announcement = row[announcement_idx] if announcement_idx < len(row) else ""
            lines.append(f"- **{fund}**（{published}）：{announcement}")
        return lines

    lines = []
    for row in rows:
        title = row[title_idx] if title_idx < len(row) else ""
        if code_idx is not None and code_idx < len(row):
            code = row[code_idx]
            if code:
                title = f"{title}（{code}）"

        parts: list[str] = []
        for index, header_text in enumerate(headers):
            if index in skip:
                continue
            cell = row[index] if index < len(row) else ""
            if not cell or (
                cell == "—"
                and header_text
                not in {"方向命中率", "平均涨跌", "平均最大不利"}
            ):
                continue
            parts.append(f"{header_text}：{cell}")

        separate_parts = any(
            "股票前" in header_text
            or "债券前" in header_text
            or header_text in {
                "行动：做什么",
                "坚持：逻辑 / 证伪",
                "眼光：质量 / 估值 / 位置",
            }
            for header_text in headers
        ) or any(len(part) > 90 for part in parts)
        if not parts:
            lines.append(f"- **{title}**")
        elif separate_parts:
            lines.append(f"- **{title}**")
            for part in parts:
                lines.append(f"  - {part}")
        elif len(parts) <= 4:
            lines.append(f"- **{title}**：" + "；".join(parts))
        else:
            lines.append(f"- **{title}**")
            for start in range(0, len(parts), 4):
                lines.append("  - " + "；".join(parts[start : start + 4]))
    return lines


def report_to_mobile_markdown(markdown: str) -> str:
    """Replace Markdown tables with bullet lists for Feishu/mobile reading."""
    lines = markdown.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and lines[index + 1].lstrip().startswith("|")
        ):
            header = _split_md_row(line)
            separator = _split_md_row(lines[index + 1])
            if _is_md_separator(separator):
                rows: list[list[str]] = []
                cursor = index + 2
                while cursor < len(lines) and lines[cursor].lstrip().startswith("|"):
                    rows.append(_split_md_row(lines[cursor]))
                    cursor += 1
                output.extend(_format_table_rows(header, rows))
                index = cursor
                continue
        output.append(line)
        index += 1

    while output and not output[0].strip():
        output.pop(0)
    if output and output[0].startswith("# "):
        output.pop(0)
        while output and not output[0].strip():
            output.pop(0)
    return "\n".join(output).strip() + "\n"


def _split_markdown_chunks(markdown: str, max_chars: int = 6000) -> list[str]:
    sections = re.split(r"(?m)(?=^## )", markdown)
    chunks: list[str] = []
    current = ""
    for section in sections:
        if not section.strip():
            continue
        if current and len(current) + len(section) > max_chars:
            chunks.append(current.rstrip())
            current = section
        else:
            current += section
    if current.strip():
        chunks.append(current.rstrip())

    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
            continue
        current_lines: list[str] = []
        current_length = 0
        for line in chunk.splitlines(keepends=True):
            if current_length + len(line) > max_chars and current_lines:
                result.append("".join(current_lines).rstrip())
                current_lines = []
                current_length = 0
            current_lines.append(line)
            current_length += len(line)
        if current_lines:
            result.append("".join(current_lines).rstrip())
    return result or [markdown.strip()]


def feishu_card_notify(
    url: str,
    title: str,
    markdown: str,
    secret: str = "",
) -> bool:
    """Send a full Markdown report as a Feishu card (schema 2.0).

    The report is split into several markdown components so that one oversized
    component does not get truncated by Feishu.
    """
    chunks = _split_markdown_chunks(markdown)
    payload = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": chunk}
                    for chunk in chunks
                ]
            },
        },
    }
    return _feishu_post(url, payload, secret=secret)


def generic_webhook_notify(url: str, title: str, message: str) -> bool:
    try:
        response = requests.post(
            url,
            json={"title": title, "body": message, "text": f"{title}\n{message}"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Webhook 通知失败: %s", exc)
        return False


def webhook_notify(url: str, title: str, message: str, secret: str = "") -> bool:
    if not url:
        return False
    if is_feishu_webhook(url):
        return feishu_notify(url, title, message, secret=secret)
    return generic_webhook_notify(url, title, message)


def notify(config: dict, title: str, message: str) -> bool:
    notify_config = config.get("notify", {})
    sent = False
    if notify_config.get("macos"):
        sent = macos_notify(title, message) or sent

    webhook_url = (notify_config.get("webhook_url") or "").strip()
    if webhook_url:
        sent = (
            webhook_notify(
                webhook_url,
                title,
                message,
                secret=_webhook_secret(notify_config),
            )
            or sent
        )
    return sent


def notify_report(
    config: dict,
    title: str,
    summary: str,
    report_text: str,
) -> bool:
    """Send a short summary to macOS and the full Markdown report to Feishu."""
    notify_config = config.get("notify", {})
    sent = False
    if notify_config.get("macos"):
        sent = macos_notify(title, summary) or sent

    webhook_url = (notify_config.get("webhook_url") or "").strip()
    if not webhook_url:
        return sent

    secret = _webhook_secret(notify_config)
    if is_feishu_webhook(webhook_url):
        card_markdown = report_to_mobile_markdown(report_text)
        ok = feishu_card_notify(webhook_url, title, card_markdown, secret=secret)
        if not ok:
            LOG.warning("飞书卡片发送失败，回退为文本完整版")
            ok = feishu_notify(webhook_url, title, report_text, secret=secret)
        return ok or sent

    return (
        generic_webhook_notify(
            webhook_url,
            title,
            f"{summary}\n\n{report_text}",
        )
        or sent
    )
