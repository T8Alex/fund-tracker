from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .macro import classify_regime

AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(module, digest_date, kind)
);
CREATE INDEX IF NOT EXISTS idx_ai_digests_date
    ON ai_digests(module, digest_date, kind);
"""

DAILY_REQUIRED = (
    "summary",
    "facts",
    "bullish",
    "bearish",
    "risks",
    "watch",
    "confidence",
    "limitations",
)
WEEKLY_REQUIRED = (
    "week_summary",
    "confirmed",
    "challenged",
    "next_week_watch",
    "risks",
    "confidence",
    "limitations",
)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LocalModelError(RuntimeError):
    """Raised when the local OpenAI-compatible model cannot be used."""


def ensure_ai_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(AI_SCHEMA)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(value: Any) -> int:
    number = _as_float(value)
    if number is None:
        return 50
    return max(0, min(100, round(number)))


def _string_list(value: Any, *, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _as_text(item, 600)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _fact_list(value: Any, *, limit: int = 8) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        fact = _as_text(item.get("fact"), 800)
        if not fact:
            continue
        source_id = item.get("source_id")
        source_text = str(source_id).strip() if source_id is not None else ""
        if source_text in {"0", "None", "null", "无"}:
            source_text = ""
        result.append(
            {
                "source_id": source_text,
                "fact": fact,
                "impact": _as_text(item.get("impact"), 800),
            }
        )
        if len(result) >= limit:
            break
    return result


def _extract_json(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise LocalModelError("本地模型返回空内容")
    raw = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```\s*$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise LocalModelError("本地模型没有返回 JSON 对象")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        raise LocalModelError(f"本地模型 JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise LocalModelError("本地模型返回的不是 JSON 对象")
    return value


def _normalise_daily(raw: dict) -> dict:
    summary = _as_text(raw.get("summary"), 3000)
    if not summary:
        raise LocalModelError("本地模型摘要为空")
    facts = _fact_list(raw.get("facts"))
    bullish = _string_list(raw.get("bullish"))
    bearish = _string_list(raw.get("bearish"))
    risks = _string_list(raw.get("risks"))
    watch = _string_list(raw.get("watch"))
    if not (facts or bullish or bearish or risks or watch):
        raise LocalModelError("本地模型没有给出可用的分析内容")
    return {
        "summary": summary,
        "facts": facts,
        "bullish": bullish,
        "bearish": bearish,
        "risks": risks,
        "watch": watch,
        "confidence": _confidence(raw.get("confidence")),
        "limitations": _as_text(raw.get("limitations"), 1200),
    }


def _normalise_weekly(raw: dict) -> dict:
    summary = _as_text(raw.get("week_summary"), 3000)
    if not summary:
        raise LocalModelError("本地模型周度摘要为空")
    result = {
        "week_summary": summary,
        "confirmed": _string_list(raw.get("confirmed")),
        "challenged": _string_list(raw.get("challenged")),
        "next_week_watch": _string_list(raw.get("next_week_watch")),
        "risks": _string_list(raw.get("risks")),
        "confidence": _confidence(raw.get("confidence")),
        "limitations": _as_text(raw.get("limitations"), 1200),
    }
    if not any(
        result[key]
        for key in ("confirmed", "challenged", "next_week_watch", "risks")
    ):
        raise LocalModelError("本地模型没有给出可用的周度分析内容")
    return result


def _compact_news(news: Iterable[dict], *, max_news: int, intro_chars: int) -> list[dict]:
    result: list[dict] = []
    for index, item in enumerate(list(news or [])[: max(1, int(max_news))], start=1):
        if not isinstance(item, dict):
            continue
        title = _as_text(item.get("title"), 300)
        if not title:
            continue
        result.append(
            {
                "id": index,
                "source": _as_text(item.get("source"), 80),
                "published": _as_text(item.get("published"), 40),
                "title": title,
                "intro": _as_text(item.get("intro"), max(60, int(intro_chars))),
                "tags": _string_list(item.get("tags"), limit=5),
            }
        )
    return result


def _fit_input(data: dict, max_chars: int | None) -> dict:
    """Keep the prompt inside a conservative local context budget.

    The compact payload is usually far below the limit; this is a safety net for
    days with many long news intros or a full week of daily digests.
    """
    limit = int(max_chars or 0)
    if limit <= 0:
        return data

    def size() -> int:
        return len(json.dumps(data, ensure_ascii=False))

    if size() <= limit:
        return data
    for item in data.get("news") or []:
        if isinstance(item, dict):
            item["intro"] = _as_text(item.get("intro"), 160)
    if size() <= limit:
        return data
    news = list(data.get("news") or [])
    while len(news) > 2 and size() > limit:
        news.pop()
    data["news"] = news
    if size() <= limit:
        return data
    digests = list(data.get("daily_digests") or [])
    while len(digests) > 2 and size() > limit:
        digests.pop(0)
    data["daily_digests"] = digests
    if size() <= limit:
        return data
    for digest in digests:
        content = digest.get("content") if isinstance(digest, dict) else None
        if isinstance(content, dict):
            for key in ("summary", "week_summary", "limitations"):
                if key in content:
                    content[key] = _as_text(content[key], 300)
    return data


def _compact_macro(macro: dict | None) -> dict:
    if not macro:
        return {}
    regime = classify_regime(macro)
    china = macro.get("china") or {}
    global_data = macro.get("global") or {}
    indices = []
    for item in macro.get("indices") or []:
        indices.append(
            {
                "name": _as_text(item.get("name"), 40),
                "pct": _as_float(item.get("pct")),
                "ret_20d": _as_float(item.get("ret_20d")),
                "trend": _as_text(item.get("trend"), 40),
            }
        )
    return {
        "regime": regime.get("summary") or "",
        "china_10y": _as_float(china.get("10y")),
        "china_10y_5d_bp": _as_float(china.get("10y_5d_bp")),
        "china_2y": _as_float(china.get("2y")),
        "us_10y": _as_float(global_data.get("us_10y")),
        "us_10y_5d_bp": _as_float(global_data.get("us_10y_5d_bp")),
        "us_2y": _as_float(global_data.get("us_2y")),
        "usdcny": _as_float((global_data.get("usdcny") or {}).get("current")),
        "oil": _as_float((global_data.get("oil") or {}).get("current")),
        "gold": _as_float((global_data.get("gold") or {}).get("current")),
        "indices": indices,
    }


def _compact_rows(rows: Iterable[dict] | None) -> list[dict]:
    result: list[dict] = []
    for row in list(rows or [])[:20]:
        evidence = row.get("evidence") or {}
        result.append(
            {
                "code": _as_text(row.get("code"), 20),
                "name": _as_text(row.get("name"), 80),
                "action": _as_text(row.get("review_action"), 40),
                "score": _as_float(row.get("score")),
                "evidence": _as_float(
                    evidence.get("score") if isinstance(evidence, dict) else None
                ),
                "target_weight": _as_float(row.get("target_weight")),
                "actual_weight": _as_float(row.get("actual_weight")),
                "market_value": _as_float(row.get("market_value")),
                "pnl": _as_float(row.get("pnl")),
                "pnl_pct": _as_float(row.get("pnl_pct")),
                "period_return": _as_float(
                    row.get("ret_window")
                    if row.get("ret_window") is not None
                    else row.get("ret_period")
                ),
            }
        )
    return result


def _load_cached(
    db_path: Path | str,
    *,
    module: str,
    digest_date: str,
    kind: str,
    input_hash: str,
) -> dict | None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        connection.row_factory = sqlite3.Row
        ensure_ai_schema(connection)
        row = connection.execute(
            """
            SELECT content_json FROM ai_digests
            WHERE module=? AND digest_date=? AND kind=? AND input_hash=?
            """,
            (module, digest_date, kind, input_hash),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["content_json"])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _save_digest(
    db_path: Path | str,
    *,
    module: str,
    digest_date: str,
    kind: str,
    model: str,
    endpoint: str,
    input_hash: str,
    content: dict,
) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        ensure_ai_schema(connection)
        connection.execute(
            """
            INSERT INTO ai_digests(
                module, digest_date, kind, model, endpoint,
                input_hash, content_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module, digest_date, kind) DO UPDATE SET
                model=excluded.model,
                endpoint=excluded.endpoint,
                input_hash=excluded.input_hash,
                content_json=excluded.content_json,
                created_at=excluded.created_at
            """,
            (
                module,
                digest_date,
                kind,
                model,
                endpoint,
                input_hash,
                json.dumps(content, ensure_ascii=False),
                _now(),
            ),
        )


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LocalModelError(f"本地模型 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalModelError(f"本地模型连接失败：{exc}") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LocalModelError("本地模型返回的不是 JSON 响应") from exc
    if not isinstance(value, dict):
        raise LocalModelError("本地模型响应格式异常")
    return value


def _get_json(url: str, timeout: float = 3.0) -> dict | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return None
    return value if isinstance(value, dict) else None


def _ensure_local_endpoint(endpoint: str) -> None:
    """Refuse non-loopback endpoints: this project must not use cloud APIs."""
    parsed = urlparse(str(endpoint or ""))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOCAL_HOSTS:
        raise LocalModelError(
            "本地模型端点必须是 127.0.0.1/localhost/::1；"
            "本项目不使用云 API。"
        )


def _api_base(endpoint: str) -> str:
    marker = "/v1/"
    if marker in endpoint:
        return endpoint.split(marker, 1)[0]
    return endpoint.rstrip("/")


def _model_loaded(endpoint: str, model: str) -> bool | None:
    """Check whether the requested model is already loaded.

    /api/ps is a metadata endpoint and does not wake the backend. None means
    the proxy does not expose enough information; in that case we leave the
    model alone and let the proxy's idle timer handle release.
    """
    data = _get_json(_api_base(endpoint) + "/api/ps")
    if not isinstance(data, dict) or "models" not in data:
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") == model or str(item.get("model") or "") == model:
            return True
    return False


def _unload_model(endpoint: str) -> None:
    """Ask the local wake proxy to release the model immediately.

    Older/local proxies may not implement /unload; the wake proxy also has an
    idle timeout, so a 404 here is harmless.
    """
    try:
        _post_json(_api_base(endpoint) + "/unload", {}, timeout=5.0)
    except Exception:  # noqa: BLE001 - release is best-effort
        pass


def _call_local(endpoint: str, payload: dict, timeout: float, *, release: bool) -> str:
    _ensure_local_endpoint(endpoint)
    model = str(payload.get("model") or "")
    was_loaded = _model_loaded(endpoint, model) if release else None
    try:
        response = _post_json(endpoint, payload, timeout)
    finally:
        if release and was_loaded is False:
            _unload_model(endpoint)
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LocalModelError("本地模型没有返回 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    text = str(content or "").strip()
    if not text:
        raise LocalModelError("本地模型只返回了 reasoning，没有正文")
    return text


def _daily_payload(
    *,
    module: str,
    digest_date: str,
    news: list[dict],
    macro: dict,
    model_cfg: dict,
) -> dict:
    input_data = {
        "module": module,
        "date": digest_date,
        "source_policy": (
            "只使用下面新闻的标题/摘要和宏观数据；没有专家原文时，"
            "只能写媒体观点或 AI 推断，不要编造专家姓名。"
        ),
        "news": news,
        "macro": macro,
    }
    schema = {
        "summary": "3-5句中文摘要",
        "facts": [
            {
                "source_id": "仅当事实来自某条新闻时填该新闻 id；宏观数据填 null",
                "fact": "材料中的事实",
                "impact": "可能影响",
            }
        ],
        "bullish": ["偏多线索，必须说明依据"],
        "bearish": ["偏空线索，必须说明依据"],
        "risks": ["风险或不确定性"],
        "watch": ["后续需要观察的指标/事件"],
        "confidence": 0,
        "limitations": "材料不足或模型不确定之处",
    }
    system = (
        "你是本项目（个人基金/股票跟踪工具）的研究助理。"
        "你的唯一任务是阅读用户提供的新闻标题、摘要和宏观数据，做事实提炼、"
        "观点归纳、风险分析。\n"
        "硬性规则：\n"
        "1. 只使用输入材料，不补充材料中没有的事实；不确定就写“材料不足”。\n"
        "2. 不编造专家姓名、机构、数据或来源；没有专家原文时只写“媒体观点”或“AI 推断”。\n"
        "3. 不提供买卖指令、目标价或收益承诺；可以描述偏多/偏空/风险。\n"
        "4. 区分事实、媒体观点和 AI 推断；宏观数据不是新闻，facts 的 source_id 填 null。\n"
        "5. 只输出一个 JSON 对象，不要 Markdown 代码围栏，不要额外解释。\n"
        "6. 输出要短：summary 不超过 120 字；facts 最多 5 条；"
        "bullish/bearish/risks/watch 每类最多 3 条，每条不超过 60 字；"
        "limitations 不超过 80 字。\n"
        "7. 这个模型只允许用于本项目的新闻/文章摘要和研究分析，不用于其他用途。"
    )
    user = (
        "请分析以下输入，并严格按给定 JSON 结构输出。\n"
        f"JSON 结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"输入：{json.dumps(input_data, ensure_ascii=False)}"
    )
    payload: dict[str, Any] = {
        "model": model_cfg.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(model_cfg.get("temperature", 0.1)),
        "max_tokens": int(model_cfg.get("max_tokens", 900)),
        "stream": False,
    }
    if model_cfg.get("enable_thinking") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _weekly_payload(
    *,
    module: str,
    digest_date: str,
    period_label: str,
    rows: list[dict],
    news: list[dict],
    daily_digests: list[dict],
    macro: dict,
    portfolio_summary: dict | None,
    model_cfg: dict,
) -> dict:
    input_data = {
        "module": module,
        "date": digest_date,
        "period": period_label,
        "portfolio_summary": portfolio_summary or {},
        "portfolio_or_watchlist": rows,
        "daily_digests": daily_digests,
        "news": news,
        "macro": macro,
        "source_policy": (
            "基于本周期已生成的每日 AI 摘要、组合表现和新闻材料；"
            "actual_weight 是用户实际仓位，target_weight 是目标仓位；"
            "actual_weight 为空表示未接入实际持仓，不能假装已有仓位；"
            "输入已含基金层实际仓位、市值和盈亏，不要笼统写“缺乏持仓数据”；"
            "不要编造专家或来源。"
        ),
    }
    schema = {
        "week_summary": "3-5句中文周度复盘",
        "confirmed": ["本周被数据/事件验证的判断"],
        "challenged": ["本周被数据/事件挑战或证伪的判断"],
        "next_week_watch": ["下周需要观察的指标/事件"],
        "risks": ["主要风险"],
        "confidence": 0,
        "limitations": "材料不足或模型不确定之处",
    }
    system = (
        "你是本项目（个人基金/股票跟踪工具）的周度研究助理。"
        "只根据输入的每日摘要、组合表现和新闻材料做复盘；"
        "区分实际仓位 actual_weight 与目标仓位 target_weight；"
        "不编造专家、来源或数据；不提供买卖指令、目标价或收益承诺；"
        "confirmed 与 challenged 不得重复；不要把组合状态本身当作被验证/被证伪的判断；"
        "没有明确证据时宁可少列；输出要短：week_summary 不超过 150 字；"
        "每个列表最多 4 条，每条不超过 60 字；只输出一个 JSON 对象，不要 Markdown 代码围栏。"
    )
    user = (
        "请做本周复盘，严格按给定 JSON 结构输出。\n"
        f"JSON 结构：{json.dumps(schema, ensure_ascii=False)}\n"
        f"输入：{json.dumps(input_data, ensure_ascii=False)}"
    )
    payload: dict[str, Any] = {
        "model": model_cfg.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(model_cfg.get("temperature", 0.1)),
        "max_tokens": int(model_cfg.get("max_tokens", 1200)),
        "stream": False,
    }
    if model_cfg.get("enable_thinking") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def build_daily_digest(
    *,
    db_path: Path | str,
    module: str,
    digest_date: str,
    news: Iterable[dict],
    macro_snapshot: dict | None,
    config: dict,
) -> dict | None:
    ai_config = config or {}
    if not ai_config.get("enabled", True):
        return None
    news_items = _compact_news(
        news,
        max_news=int(ai_config.get("max_news", 8)),
        intro_chars=int(ai_config.get("intro_chars", 500)),
    )
    if not news_items:
        return None
    model_cfg = ai_config.get("daily") or {}
    model = str(model_cfg.get("model") or "")
    endpoint = str(model_cfg.get("endpoint") or "")
    if not model or not endpoint:
        raise LocalModelError("本地模型配置缺少 model 或 endpoint")
    compact_macro = _compact_macro(macro_snapshot)
    input_data = {
        "module": module,
        "date": digest_date,
        "news": news_items,
        "macro": compact_macro,
        "model": model,
    }
    input_data = _fit_input(input_data, ai_config.get("max_input_chars"))
    input_hash = _sha256(input_data)
    cached = _load_cached(
        db_path,
        module=module,
        digest_date=digest_date,
        kind="daily",
        input_hash=input_hash,
    )
    if cached:
        return {
            "content": cached,
            "model": model,
            "endpoint": endpoint,
            "cached": True,
            "news": input_data["news"],
        }
    payload = _daily_payload(
        module=module,
        digest_date=digest_date,
        news=input_data["news"],
        macro=input_data["macro"],
        model_cfg=model_cfg,
    )
    text = _call_local(
        endpoint,
        payload,
        float(model_cfg.get("timeout_seconds", 120)),
        release=bool(ai_config.get("release_after_call", True)),
    )
    content = _normalise_daily(_extract_json(text))
    valid_source_ids = {str(item.get("id")) for item in input_data["news"]}
    for fact in content["facts"]:
        if fact.get("source_id") and fact["source_id"] not in valid_source_ids:
            fact["source_id"] = ""
    _save_digest(
        db_path,
        module=module,
        digest_date=digest_date,
        kind="daily",
        model=model,
        endpoint=endpoint,
        input_hash=input_hash,
        content=content,
    )
    return {
        "content": content,
        "model": model,
        "endpoint": endpoint,
        "cached": False,
        "news": input_data["news"],
    }


def _load_daily_digests(
    db_path: Path | str,
    *,
    module: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        connection.row_factory = sqlite3.Row
        ensure_ai_schema(connection)
        rows = connection.execute(
            """
            SELECT digest_date, content_json FROM ai_digests
            WHERE module=? AND kind='daily' AND digest_date>=? AND digest_date<=?
            ORDER BY digest_date
            """,
            (module, start_date, end_date),
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        try:
            content = json.loads(row["content_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(content, dict):
            result.append({"date": row["digest_date"], "content": content})
    return result


def build_weekly_digest(
    *,
    db_path: Path | str,
    module: str,
    digest_date: str,
    period_start: str,
    period_end: str,
    period_label: str,
    rows: Iterable[dict] | None,
    news: Iterable[dict],
    macro_snapshot: dict | None,
    config: dict,
) -> dict | None:
    ai_config = config or {}
    if not ai_config.get("enabled", True):
        return None
    if not (ai_config.get("weekly") or {}).get("enabled", True):
        return None
    daily_digests = _load_daily_digests(
        db_path, module=module, start_date=period_start, end_date=period_end
    )
    news_items = _compact_news(
        news,
        max_news=int(ai_config.get("max_news", 8)),
        intro_chars=int(ai_config.get("intro_chars", 500)),
    )
    if not daily_digests and not news_items:
        return None
    model_cfg = ai_config.get("weekly") or {}
    model = str(model_cfg.get("model") or "")
    endpoint = str(model_cfg.get("endpoint") or "")
    if not model or not endpoint:
        raise LocalModelError("周度本地模型配置缺少 model 或 endpoint")
    compact_macro = _compact_macro(macro_snapshot)
    compact_rows = _compact_rows(rows)
    portfolio_summary = {
        "actual_positions": sum(
            1
            for row in compact_rows
            if (row.get("actual_weight") or 0) > 0
        ),
        "actual_value": round(
            sum(row.get("market_value") or 0 for row in compact_rows),
            2,
        ),
        "actual_pnl": round(
            sum(row.get("pnl") or 0 for row in compact_rows),
            2,
        ),
    }
    input_data = {
        "module": module,
        "date": digest_date,
        "prompt_version": "weekly-v3",
        "period_start": period_start,
        "period_end": period_end,
        "period_label": period_label,
        "portfolio_summary": portfolio_summary,
        "rows": compact_rows,
        "daily_digests": daily_digests,
        "news": news_items,
        "macro": compact_macro,
        "model": model,
    }
    input_data = _fit_input(input_data, ai_config.get("max_input_chars"))
    input_hash = _sha256(input_data)
    cached = _load_cached(
        db_path,
        module=module,
        digest_date=digest_date,
        kind="weekly",
        input_hash=input_hash,
    )
    if cached:
        return {
            "content": cached,
            "model": model,
            "endpoint": endpoint,
            "cached": True,
            "source_days": len(daily_digests),
        }
    payload = _weekly_payload(
        module=module,
        digest_date=digest_date,
        period_label=period_label,
        rows=input_data["rows"],
        news=input_data["news"],
        daily_digests=input_data["daily_digests"],
        macro=input_data["macro"],
        portfolio_summary=input_data.get("portfolio_summary"),
        model_cfg=model_cfg,
    )
    text = _call_local(
        endpoint,
        payload,
        float(model_cfg.get("timeout_seconds", 240)),
        release=bool(ai_config.get("release_after_call", True)),
    )
    content = _normalise_weekly(_extract_json(text))
    _save_digest(
        db_path,
        module=module,
        digest_date=digest_date,
        kind="weekly",
        model=model,
        endpoint=endpoint,
        input_hash=input_hash,
        content=content,
    )
    return {
        "content": content,
        "model": model,
        "endpoint": endpoint,
        "cached": False,
        "source_days": len(daily_digests),
    }


def _source_lines(news: Iterable[dict]) -> list[str]:
    lines: list[str] = []
    for item in news or []:
        source_id = item.get("id")
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        published = str(item.get("published") or "").strip()
        if not title:
            continue
        meta = " ".join(part for part in (source, published) if part)
        suffix = f"（{meta}）" if meta else ""
        lines.append(f"- [{source_id}] {title}{suffix}")
    return lines


def _bullet_block(title: str, items: Iterable[str]) -> list[str]:
    values = [str(item).strip() for item in items or [] if str(item).strip()]
    if not values:
        return []
    lines = [f"**{title}**", ""]
    lines.extend(f"- {value}" for value in values)
    lines.append("")
    return lines


def render_ai_section(result: dict, *, module: str = "") -> str:
    content = result.get("content") or {}
    model = str(result.get("model") or "本地模型")
    cached = bool(result.get("cached"))
    news = result.get("news") or []
    lines = [
        "## AI 新闻与文章摘要（本地模型）",
        "",
        f"> 模型：{model}（本机）；只基于本报告抓取的新闻标题/摘要与宏观数据做归纳。"
        "事实、媒体观点和 AI 推断已尽量分开；AI 可能出错，以原始来源为准。"
        "不构成投资建议，不提供买卖指令。",
    ]
    if cached:
        lines.append("> 本次复用当日已生成的摘要，没有重复调用模型。")
    lines.extend(["", f"**摘要：** {content.get('summary') or '—'}", ""])
    facts = content.get("facts") or []
    if facts:
        lines.append("**材料事实与影响（AI 提炼，未逐条人工核对）：**")
        lines.append("")
        for fact in facts:
            source_id = str(fact.get("source_id") or "").strip()
            prefix = f"[{source_id}] " if source_id else ""
            impact = str(fact.get("impact") or "").strip()
            suffix = f" → {impact}" if impact else ""
            lines.append(f"- {prefix}{fact.get('fact')}{suffix}")
        lines.append("")
    lines.extend(_bullet_block("偏多线索（AI 推断）", content.get("bullish") or []))
    lines.extend(_bullet_block("偏空线索（AI 推断）", content.get("bearish") or []))
    lines.extend(_bullet_block("风险与不确定性（AI 推断）", content.get("risks") or []))
    lines.extend(_bullet_block("后续观察（AI 推断）", content.get("watch") or []))
    sources = _source_lines(news)
    if sources:
        lines.extend(["**本次输入来源：**", ""])
        lines.extend(sources)
        lines.append("")
    lines.append(
        f"**AI 置信度：** {content.get('confidence', '—')}/100。"
        "这只是模型对材料一致性的自评，不是胜率。"
    )
    limitations = str(content.get("limitations") or "").strip()
    if limitations:
        lines.append("")
        lines.append(f"**局限：** {limitations}")
    lines.append("")
    return "\n".join(lines)


def render_weekly_ai_section(result: dict, *, module: str = "") -> str:
    content = result.get("content") or {}
    model = str(result.get("model") or "本地模型")
    cached = bool(result.get("cached"))
    lines = [
        "## AI 周度复盘（本地模型）",
        "",
        f"> 模型：{model}（本机）；基于本周期已生成的每日摘要、组合表现（含实际/目标仓位）和新闻材料。"
        "AI 可能出错，以原始来源和数据为准；不构成投资建议。",
    ]
    if cached:
        lines.append("> 本次复用本周期已生成的复盘，没有重复调用模型。")
    lines.extend(["", f"**本周摘要：** {content.get('week_summary') or '—'}", ""])
    lines.extend(_bullet_block("AI 认为被验证的判断", content.get("confirmed") or []))
    lines.extend(_bullet_block("AI 认为被挑战/证伪的判断", content.get("challenged") or []))
    lines.extend(_bullet_block("下周观察（AI 推断）", content.get("next_week_watch") or []))
    lines.extend(_bullet_block("主要风险（AI 推断）", content.get("risks") or []))
    lines.append(
        f"**AI 置信度：** {content.get('confidence', '—')}/100。"
        "这是模型对材料一致性的自评，不是胜率。"
    )
    limitations = str(content.get("limitations") or "").strip()
    if limitations:
        lines.append("")
        lines.append(f"**局限：** {limitations}")
    lines.append("")
    return "\n".join(lines)


def render_ai_error(error: Exception | str) -> str:
    message = str(error or "未知错误").strip()
    return (
        "## AI 新闻与文章摘要（本地模型）\n\n"
        f"> 本次未生成：{message}。报告的其他部分不受影响；"
        "本地模型空闲后会自动释放内存。\n\n"
    )
