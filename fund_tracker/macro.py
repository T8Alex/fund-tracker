from __future__ import annotations

import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from .fetch import (
    INDEX_SECIDS,
    FetchError,
    HttpClient,
    _to_float,
    fetch_index_history,
)
from .metrics import price_technicals

LOG = logging.getLogger(__name__)

SINA_GLOBAL_SYMBOLS = {
    "gb_$dji": ("道琼斯", "index"),
    "gb_ixic": ("纳斯达克", "index"),
    "int_hangseng": ("恒生指数", "index"),
    "int_nikkei": ("日经225", "index"),
    "fx_susdcny": ("美元/人民币", "fx"),
    "hf_GC": ("COMEX黄金", "commodity"),
    "hf_CL": ("WTI原油", "commodity"),
}

SINA_ROLL_URL = (
    "https://feed.mix.sina.com.cn/api/roll/get"
    "?pageid=153&lid={lid}&k=&num=30&page=1"
)

NEWS_TAG_RULES = [
    (
        (
            "美联储",
            "加息",
            "降息",
            "fomc",
            "fed",
            "联邦基金",
            "日本央行",
            "欧洲央行",
            "英国央行",
            "俄罗斯央行",
        ),
        "海外利率",
    ),
    (("通胀", "cpi", "物价", "pce"), "通胀"),
    (("关税", "贸易", "出口", "进口", "tariff"), "贸易"),
    (("地缘", "冲突", "战争", "中东", "俄乌", "乌克兰", "以色列", "恐怖"), "地缘"),
    (("原油", "石油", "opec", "油价"), "油价"),
    (("人民币", "汇率", "美元指数", "美元"), "汇率"),
    (("房地产", "地产", "楼市", "房价"), "地产"),
    (("地方债", "化债", "城投", "财政"), "信用/财政"),
    (("人工智能", "ai", "芯片", "半导体", "算力"), "科技成长"),
    (("降准", "mlf", "lpr", "中国央行", "人民银行", "中国货币政策", "政策利率"), "国内政策"),
    (("国债", "收益率", "债市", "债券"), "利率"),
    (("a股", "港股", "美股", "股市", "风险偏好"), "风险偏好"),
]

MACRO_NEWS_PRIORITY = [
    "海外利率",
    "通胀",
    "地缘",
    "油价",
    "汇率",
    "国内政策",
    "信用/财政",
    "贸易",
    "利率",
    "地产",
    "风险偏好",
    "科技成长",
]

TAG_IMPACT = {
    "海外利率": "美债利率上行/美联储偏鹰时，成长股估值承压，国内债市也会受外部约束。",
    "通胀": "通胀上行不利于长债，利好资源/黄金；通胀回落则相反。",
    "贸易": "关税/贸易摩擦会扰动出口链和风险偏好，宽基与成长更敏感。",
    "地缘": "地缘冲突推升油价和避险需求，权益波动加大，黄金受益。",
    "油价": "油价高企推升通胀预期，长债和成长股更敏感；油价回落则缓解。",
    "汇率": "人民币走弱时外资敏感板块承压，红利/内需相对防御。",
    "地产": "地产链企稳利好信用债和宽基；继续走弱则信用风险上升。",
    "信用/财政": "地方债/城投风险影响信用债和二级债，重点看信用利差。",
    "科技成长": "AI/半导体新闻影响成长风格，波动大，不适合追高。",
    "国内政策": "降准降息/财政发力利好债市和宽基，但要判断是否已被定价。",
    "利率": "国债收益率上行压制债基净值；下行则相反。",
    "风险偏好": "风险偏好上升时成长/宽基占优，红利可能跑输；下降时相反。",
}

CHINA_BOND_TREE_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/queryTree?locale=zh_CN"
CHINA_BOND_YC_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc"
CHINA_BOND_DEFAULT_ID = "2c9081e50a2f9606010a3068cae70001"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _fmt_bp(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}bp"


def _fmt_yield(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def _last_weekdays(count: int = 12) -> list[date]:
    days: list[date] = []
    current = date.today()
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return list(reversed(days))


def _parse_sina_quote(symbol: str, payload: str) -> dict | None:
    fields = payload.split(",")
    if len(fields) < 4 or not fields[0]:
        return None
    if symbol.startswith("gb_"):
        current = _to_float(fields[1])
        pct = _to_float(fields[2])
        change = _to_float(fields[4])
        return {
            "name": fields[0],
            "current": current,
            "pct": pct,
            "change": change,
            "date": fields[3] if len(fields) > 3 else "",
        }
    if symbol.startswith("int_"):
        current = _to_float(fields[1])
        change = _to_float(fields[2])
        pct = _to_float(fields[3])
        return {
            "name": fields[0],
            "current": current,
            "pct": pct,
            "change": change,
            "date": "",
        }
    if symbol.startswith("fx_"):
        current = _to_float(fields[8]) if len(fields) > 8 else None
        prev = _to_float(fields[3]) if len(fields) > 3 else None
        pct = _to_float(fields[10]) if len(fields) > 10 else None
        if current is None:
            current = _to_float(fields[1])
        if pct is None and current is not None and prev:
            pct = (current / prev - 1) * 100
        return {
            "name": fields[9] if len(fields) > 9 and fields[9] else "美元/人民币",
            "current": current,
            "pct": pct,
            "change": None,
            "date": fields[0] if fields else "",
        }
    if symbol.startswith("hf_"):
        current = _to_float(fields[0])
        prev = _to_float(fields[7]) if len(fields) > 7 else None
        pct = (current / prev - 1) * 100 if current is not None and prev else None
        return {
            "name": fields[13] if len(fields) > 13 and fields[13] else symbol,
            "current": current,
            "pct": pct,
            "change": None,
            "date": fields[12] if len(fields) > 12 else "",
        }
    return None


def fetch_global_quotes(client: HttpClient) -> dict[str, dict]:
    url = "https://hq.sinajs.cn/list=" + ",".join(SINA_GLOBAL_SYMBOLS)
    try:
        text = client.get(
            url,
            cache_key="macro:sina-global",
            cache_hours=1,
            encoding="gbk",
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
    except FetchError:
        return {}
    result: dict[str, dict] = {}
    for line in text.splitlines():
        match = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if not match:
            continue
        symbol, payload = match.group(1), match.group(2)
        quote = _parse_sina_quote(symbol, payload)
        if not quote:
            continue
        key = {
            "gb_$dji": "dow",
            "gb_ixic": "nasdaq",
            "int_hangseng": "hangseng",
            "int_nikkei": "nikkei",
            "fx_susdcny": "usdcny",
            "hf_GC": "gold",
            "hf_CL": "oil",
        }.get(symbol)
        if key:
            result[key] = quote
    return result


def _parse_treasury_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for row in reader:
        date_text = (row.get("Date") or "").strip()
        try:
            day = datetime.strptime(date_text, "%m/%d/%Y").date()
        except ValueError:
            continue
        rows.append(
            {
                "date": day,
                "2y": _to_float(row.get("2 Yr")),
                "10y": _to_float(row.get("10 Yr")),
                "30y": _to_float(row.get("30 Yr")),
            }
        )
    rows.sort(key=lambda item: item["date"])
    return rows


def fetch_us_treasury(client: HttpClient) -> dict:
    today = date.today()
    all_rows: list[dict] = []
    for year in (today.year - 1, today.year):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            "interest-rates/daily-treasury-rates.csv/"
            f"{year}/all?type=daily_treasury_yield_curve"
            f"&field_tdr_date_value={year}&page&_format=csv"
        )
        try:
            text = client.get(
                url,
                cache_key=f"macro:ustreasury:{year}",
                cache_hours=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        except FetchError:
            continue
        all_rows.extend(_parse_treasury_csv(text))
    if not all_rows:
        return {}
    # Deduplicate by date.
    by_date = {row["date"]: row for row in all_rows}
    rows = [by_date[key] for key in sorted(by_date)]
    latest = rows[-1]
    base = rows[-6] if len(rows) >= 6 else rows[0]
    ten_2 = None
    if latest.get("10y") is not None and latest.get("2y") is not None:
        ten_2 = (latest["10y"] - latest["2y"]) * 100
    base_ten_2 = None
    if base.get("10y") is not None and base.get("2y") is not None:
        base_ten_2 = (base["10y"] - base["2y"]) * 100
    return {
        "date": latest["date"].isoformat(),
        "2y": latest.get("2y"),
        "10y": latest.get("10y"),
        "30y": latest.get("30y"),
        "10y_2y_bp": round(ten_2, 1) if ten_2 is not None else None,
        "10y_2y_5d_bp": (
            round(ten_2 - base_ten_2, 1)
            if ten_2 is not None and base_ten_2 is not None
            else None
        ),
        "10y_5d_bp": (
            round((latest["10y"] - base["10y"]) * 100, 1)
            if latest.get("10y") is not None and base.get("10y") is not None
            else None
        ),
        "2y_5d_bp": (
            round((latest["2y"] - base["2y"]) * 100, 1)
            if latest.get("2y") is not None and base.get("2y") is not None
            else None
        ),
    }


def _china_bond_curve_id(client: HttpClient) -> str:
    try:
        text = client.post(
            CHINA_BOND_TREE_URL,
            cache_key="macro:chinabond-tree",
            cache_hours=24,
            headers={"Referer": "https://yield.chinabond.com.cn/"},
        )
        nodes = json.loads(text)
    except (FetchError, json.JSONDecodeError):
        return CHINA_BOND_DEFAULT_ID
    for node in nodes:
        if (
            isinstance(node, dict)
            and "中债国债收益率曲线" in str(node.get("name") or "")
            and str(node.get("isParent")).lower() == "false"
        ):
            return str(node.get("id") or CHINA_BOND_DEFAULT_ID)
    return CHINA_BOND_DEFAULT_ID


def fetch_china_bond(client: HttpClient) -> dict:
    days = _last_weekdays(6)
    curve_id = _china_bond_curve_id(client)
    params = {
        "xyzSelect": "txym",
        "workTimes": ",".join(day.isoformat() for day in days),
        "dxbj": "0",
        "qxll": "0,",
        "yqqxN": "N",
        "yqqxK": "K",
        "ycDefIds": curve_id,
        "locale": "zh_CN",
    }
    try:
        text = client.post(
            CHINA_BOND_YC_URL + "?" + urlencode(params),
            cache_key=f"macro:chinabond-yc:{days[-1].isoformat()}:{len(days)}:{curve_id}",
            cache_hours=6,
            headers={"Referer": "https://yield.chinabond.com.cn/"},
        )
        payload = json.loads(text)
    except (FetchError, json.JSONDecodeError):
        return {}
    rows: list[dict] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        worktime = str(item.get("worktime") or "")[:10]
        series = item.get("seriesData") or []
        points = {}
        for point in series:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                tenor = float(point[0])
                value = float(point[1])
            except (TypeError, ValueError):
                continue
            if abs(tenor - 2.0) < 1e-6:
                points["2y"] = value
            elif abs(tenor - 10.0) < 1e-6:
                points["10y"] = value
            elif abs(tenor - 30.0) < 1e-6:
                points["30y"] = value
        if points:
            rows.append({"date": worktime, **points})
    if not rows:
        return {}
    rows.sort(key=lambda item: item["date"])
    latest = rows[-1]
    base = rows[-6] if len(rows) >= 6 else rows[0]
    ten_2 = None
    if latest.get("10y") is not None and latest.get("2y") is not None:
        ten_2 = (latest["10y"] - latest["2y"]) * 100
    return {
        "date": latest["date"],
        "2y": latest.get("2y"),
        "10y": latest.get("10y"),
        "30y": latest.get("30y"),
        "10y_2y_bp": round(ten_2, 1) if ten_2 is not None else None,
        "10y_5d_bp": (
            round((latest["10y"] - base["10y"]) * 100, 1)
            if latest.get("10y") is not None and base.get("10y") is not None
            else None
        ),
        "2y_5d_bp": (
            round((latest["2y"] - base["2y"]) * 100, 1)
            if latest.get("2y") is not None and base.get("2y") is not None
            else None
        ),
    }


def fetch_index_technicals(client: HttpClient) -> list[dict]:
    result: list[dict] = []
    for name, secid in INDEX_SECIDS.items():
        try:
            rows = fetch_index_history(client, secid, days=120)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("指数技术面获取失败 %s: %s", name, exc)
            continue
        if len(rows) < 2:
            continue
        series: list[tuple[date, float]] = []
        for row in rows:
            try:
                day = date.fromisoformat(str(row.get("date"))[:10])
            except (TypeError, ValueError):
                continue
            close = row.get("close")
            if close is None:
                continue
            series.append((day, float(close)))
        if len(series) < 2:
            continue
        tech = price_technicals(series)
        latest = rows[-1]
        base5 = rows[-6] if len(rows) >= 6 else rows[0]
        pct_5d = (
            (latest["close"] / base5["close"] - 1) * 100
            if base5.get("close")
            else None
        )
        result.append(
            {
                "name": name,
                "current": latest["close"],
                "pct": latest.get("pct"),
                "pct_5d": round(pct_5d, 2) if pct_5d is not None else None,
                "date": latest.get("date"),
                "tech": tech,
            }
        )
    return result


def _news_tags(title: str, intro: str = "") -> list[str]:
    # Use the headline only. Intros often mention many unrelated macro words
    # and would create noisy tags.
    text = title.lower()
    tags: list[str] = []
    for keywords, tag in NEWS_TAG_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            tags.append(tag)
    return list(dict.fromkeys(tags))


def _parse_rss_items(
    text: str,
    source: str,
    *,
    limit: int = 8,
) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    result: list[dict] = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        published = ""
        timestamp = 0
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                published = dt.strftime("%m-%d %H:%M")
                timestamp = int(dt.timestamp())
            except (TypeError, ValueError, OverflowError):
                published = pub
        result.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "published": published,
                "timestamp": timestamp,
                "intro": "",
                "tags": _news_tags(title),
            }
        )
    return result


def fetch_news(client: HttpClient, limit: int = 8) -> list[dict]:
    items: list[dict] = []

    # Sina finance rolling news: Chinese headlines, current global markets.
    for lid in (2516, 2517):
        try:
            text = client.get(
                SINA_ROLL_URL.format(lid=lid),
                cache_key=f"macro:news:sina:{lid}",
                cache_hours=1,
                headers={"Referer": "https://finance.sina.com.cn/"},
            )
            payload = json.loads(text)
        except (FetchError, json.JSONDecodeError):
            continue
        for item in (payload.get("result") or {}).get("data") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            ctime = _to_float(item.get("ctime")) or 0
            try:
                published = datetime.fromtimestamp(ctime).strftime("%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                published = ""
            items.append(
                {
                    "title": title,
                    "url": str(item.get("url") or ""),
                    "source": str(item.get("media_name") or "新浪财经"),
                    "published": published,
                    "timestamp": int(ctime),
                    "intro": str(item.get("intro") or "")[:180],
                    "tags": _news_tags(title, str(item.get("intro") or "")),
                }
            )

    # Official / international feeds.
    feeds = [
        (
            "美联储",
            "https://www.federalreserve.gov/feeds/press_all.xml",
            6,
        ),
        (
            "CNBC",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            6,
        ),
    ]
    for source, url, feed_limit in feeds:
        try:
            text = client.get(
                url,
                cache_key=f"macro:news:{source}",
                cache_hours=2,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        except FetchError:
            continue
        items.extend(_parse_rss_items(text, source, limit=feed_limit))

    # Deduplicate and rank: macro-relevant tags first, Chinese sources
    # before English official feeds, then newest.
    def rank(item: dict) -> tuple:
        tags = item.get("tags") or []
        priorities = [
            MACRO_NEWS_PRIORITY.index(tag)
            for tag in tags
            if tag in MACRO_NEWS_PRIORITY
        ]
        priority = min(priorities) if priorities else 99
        source_priority = 0 if item.get("source") not in {"CNBC", "美联储"} else 1
        return (priority, source_priority, -(item.get("timestamp") or 0))

    seen: set[str] = set()
    unique: list[dict] = []
    source_counts: dict[str, int] = {}
    for item in sorted(items, key=rank):
        key = re.sub(r"\s+", "", item["title"])[:80]
        if key in seen:
            continue
        source = str(item.get("source") or "")
        if source in {"CNBC", "美联储"} and source_counts.get(source, 0) >= 1:
            continue
        seen.add(key)
        source_counts[source] = source_counts.get(source, 0) + 1
        unique.append(item)
    return unique[:limit]


def fetch_macro_snapshot(
    client: HttpClient,
    *,
    news_limit: int = 8,
    include_news: bool = True,
) -> dict:
    errors: list[str] = []
    china: dict[str, Any] = {}
    global_quotes: dict[str, Any] = {}
    us_treasury: dict[str, Any] = {}
    indices: list[dict] = []
    news: list[dict] = []

    try:
        china = fetch_china_bond(client)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"中国国债收益率: {exc}")
    try:
        global_quotes = fetch_global_quotes(client)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"全球行情: {exc}")
    try:
        us_treasury = fetch_us_treasury(client)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"美国国债: {exc}")
    try:
        indices = fetch_index_technicals(client)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"指数技术面: {exc}")
    if include_news:
        try:
            news = fetch_news(client, limit=news_limit)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"新闻: {exc}")

    return {
        "as_of": date.today().isoformat(),
        "china": china,
        "global": {
            "us_2y": us_treasury.get("2y"),
            "us_10y": us_treasury.get("10y"),
            "us_30y": us_treasury.get("30y"),
            "us_10y_2y_bp": us_treasury.get("10y_2y_bp"),
            "us_10y_2y_5d_bp": us_treasury.get("10y_2y_5d_bp"),
            "us_10y_5d_bp": us_treasury.get("10y_5d_bp"),
            "us_2y_5d_bp": us_treasury.get("2y_5d_bp"),
            **global_quotes,
        },
        "indices": indices,
        "news": news,
        "errors": errors,
    }


def classify_regime(macro: dict) -> dict:
    china = macro.get("china") or {}
    global_data = macro.get("global") or {}
    indices = {
        item.get("name"): item
        for item in macro.get("indices") or []
        if item.get("name")
    }
    csi = (indices.get("沪深300") or {}).get("tech") or {}
    dividend = (indices.get("红利指数") or {}).get("tech") or {}
    growth = (indices.get("创业板指") or {}).get("tech") or {}
    labels: list[str] = []
    impacts: list[str] = []

    cn10 = china.get("10y")
    cn10_chg = china.get("10y_5d_bp")
    us10 = global_data.get("us_10y")
    us10_chg = global_data.get("us_10y_5d_bp")
    usdcny = (global_data.get("usdcny") or {}).get("current")
    oil = (global_data.get("oil") or {}).get("current")

    if cn10 is not None:
        labels.append("国内低利率" if cn10 < 1.8 else "国内利率偏高")
    if cn10_chg is not None:
        if cn10_chg >= 5:
            labels.append("国内利率近5日上行")
        elif cn10_chg <= -5:
            labels.append("国内利率近5日下行")
    if us10 is not None:
        labels.append("海外高利率" if us10 >= 4.0 else "海外利率回落")
    if us10_chg is not None:
        if us10_chg >= 10:
            labels.append("海外利率近5日上行")
        elif us10_chg <= -10:
            labels.append("海外利率近5日下行")
    if usdcny is not None:
        labels.append("人民币偏弱" if usdcny > 7.05 else "人民币偏稳")
    if oil is not None and oil > 90:
        labels.append("油价偏高")
    if csi.get("trend"):
        labels.append(f"A股{csi['trend']}")
    d20 = dividend.get("ret_20d")
    g20 = growth.get("ret_20d")
    if d20 is not None and g20 is not None:
        labels.append("红利占优" if d20 > g20 else "成长占优")

    if cn10 is not None and cn10 < 1.8:
        impacts.append("短债/纯债底仓仍有支撑；但 10Y 已低，新增长久期要慢。")
    if cn10_chg is not None and cn10_chg >= 5:
        impacts.append("国内利率近 5 日上行，纯债/信用债不要追高，优先短久期。")
    if us10 is not None and us10 >= 4.0:
        impacts.append("海外高利率压制成长估值，二级债和权益不追高。")
    if us10_chg is not None and us10_chg >= 10:
        impacts.append("美债利率快速上行，全球风险偏好容易受压。")
    if oil is not None and oil > 90:
        impacts.append("油价偏高，通胀/地缘风险仍在，长债和成长波动加大。")
    if usdcny is not None and usdcny > 7.05:
        impacts.append("人民币偏弱，外资敏感板块承压，红利/内需相对防御。")
    if csi.get("trend") == "偏强":
        impacts.append("A股趋势偏强，权益仓可持有，但只按区间分批，不追涨。")
    elif csi.get("trend") in {"偏弱", "回调"}:
        impacts.append("A股趋势偏弱/回调，权益仓放慢，等企稳信号。")
    if d20 is not None and g20 is not None:
        if d20 > g20:
            impacts.append("近 20 日红利强于成长，防御风格占优。")
        else:
            impacts.append("近 20 日成长强于红利，风险偏好偏高。")
    if not impacts:
        impacts.append("宏观信号不完整，按原目标权重执行，不做方向性加仓。")

    return {
        "labels": labels,
        "summary": " ｜ ".join(labels) if labels else "数据不足",
        "impacts": impacts,
    }


def build_macro_section(macro: dict, config: dict | None = None) -> str:
    if not macro:
        return ""
    regime = classify_regime(macro)
    china = macro.get("china") or {}
    global_data = macro.get("global") or {}
    indices = macro.get("indices") or []
    news = macro.get("news") or []

    lines = [
        "## 二、宏观与外部环境",
        "",
        f"**当前状态：** {regime['summary']}。",
        "",
        "### 1. 关键指标",
        "",
        "| 指标 | 最新 | 5 日变化 | 对组合的读法 |",
        "|---|---:|---:|---|",
    ]
    lines.append(
        f"| 中国 10Y 国债 | {_fmt_yield(china.get('10y'))} | "
        f"{_fmt_bp(china.get('10y_5d_bp'))} | "
        "债底仍有支撑；若快速上行，纯债/信用债放慢加仓 |"
    )
    lines.append(
        f"| 中国 2Y 国债 | {_fmt_yield(china.get('2y'))} | "
        f"{_fmt_bp(china.get('2y_5d_bp'))} | "
        "短端利率低，短债/货币的票息空间有限 |"
    )
    lines.append(
        f"| 美国 10Y 国债 | {_fmt_yield(global_data.get('us_10y'))} | "
        f"{_fmt_bp(global_data.get('us_10y_5d_bp'))} | "
        "海外高利率压制成长估值 |"
    )
    lines.append(
        f"| 美国 2Y 国债 | {_fmt_yield(global_data.get('us_2y'))} | "
        f"{_fmt_bp(global_data.get('us_2y_5d_bp'))} | "
        "短端高利率，美联储降息预期反复 |"
    )
    ten_two_bp = global_data.get("us_10y_2y_bp")
    lines.append(
        f"| 美债 10Y-2Y | "
        f"{_fmt_num(ten_two_bp / 100, 2) if ten_two_bp is not None else '—'}% | "
        f"{_fmt_bp(global_data.get('us_10y_2y_5d_bp'))} | "
        "倒挂/陡峭化反映衰退与通胀预期 |"
    )
    usdcny = global_data.get("usdcny") or {}
    lines.append(
        f"| 美元/人民币 | {_fmt_num(usdcny.get('current'), 4)} | "
        f"{_fmt_pct(usdcny.get('pct'))} | "
        "人民币偏弱时外资敏感板块承压 |"
    )
    gold = global_data.get("gold") or {}
    oil = global_data.get("oil") or {}
    lines.append(
        f"| COMEX 黄金 | {_fmt_num(gold.get('current'), 1)} | "
        f"{_fmt_pct(gold.get('pct'))} | 避险需求与实际利率的镜子 |"
    )
    lines.append(
        f"| WTI 原油 | {_fmt_num(oil.get('current'), 2)} | "
        f"{_fmt_pct(oil.get('pct'))} | 油价高企推升通胀/地缘风险 |"
    )
    lines.append("")

    if indices:
        lines.extend(
            [
                "### 2. 大盘与 K 线",
                "",
                "| 指数 | 收盘 | 5 日 | 20 日 | MA20 | MA60 | RSI14 | 趋势 |",
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for item in indices:
            tech = item.get("tech") or {}
            lines.append(
                f"| {item.get('name')} | {_fmt_num(item.get('current'))} | "
                f"{_fmt_pct(item.get('pct_5d'))} | {_fmt_pct(tech.get('ret_20d'))} | "
                f"{_fmt_num(tech.get('ma20'))} | {_fmt_num(tech.get('ma60'))} | "
                f"{_fmt_num(tech.get('rsi14'), 1)} | {tech.get('trend', '—')} |"
            )
        lines.extend(
            [
                "",
                "> K 线只用来判断“趋势/位置”，不是预测。RSI>70 偏热、<30 偏冷；"
                "价格在 MA20/MA60 上方只代表趋势较强，不代表不会回落。"
                "单日大涨/大跌不改变趋势判断；在均线下方时，单日反弹先按反弹看。",
                "",
            ]
        )

    lines.extend(["### 3. 国际形势与新闻", ""])
    lines.append(
        f"- 美国 10Y {_fmt_num(global_data.get('us_10y'), 2)}%、"
        f"2Y {_fmt_num(global_data.get('us_2y'), 2)}%，"
        f"10Y-2Y {_fmt_bp(global_data.get('us_10y_2y_bp'))}；"
        "美联储路径仍是全球风险资产的主要外部变量。"
    )
    lines.append(
        f"- 美元/人民币 {_fmt_num(usdcny.get('current'), 4)}；"
        f"黄金 {_fmt_num(gold.get('current'), 1)}；"
        f"WTI 原油 {_fmt_num(oil.get('current'), 2)}。"
    )
    global_indices = [
        ("道琼斯", global_data.get("dow")),
        ("纳斯达克", global_data.get("nasdaq")),
        ("恒生指数", global_data.get("hangseng")),
        ("日经225", global_data.get("nikkei")),
    ]
    global_text = "、".join(
        f"{name} {_fmt_pct((quote or {}).get('pct'))}"
        for name, quote in global_indices
        if quote
    )
    if global_text:
        lines.append(f"- 全球股指：{global_text}。")
    lines.append("")

    if news:
        lines.append("**新闻与事件（只列事实标题，不把标题当结论）：**")
        lines.append("")
        for item in news[:8]:
            tags = "、".join(item.get("tags") or []) or "综合"
            source = item.get("source") or ""
            published = item.get("published") or ""
            title = item.get("title") or ""
            url = item.get("url") or ""
            suffix = f"（{source} {published}）" if source or published else ""
            if url:
                lines.append(f"- [{title}]({url}){suffix} `{tags}`")
            else:
                lines.append(f"- {title}{suffix} `{tags}`")
        lines.append("")
        tag_set: list[str] = []
        for item in news:
            for tag in item.get("tags") or []:
                if tag not in tag_set:
                    tag_set.append(tag)
        if tag_set:
            lines.append("**事件映射：**")
            lines.append("")
            for tag in tag_set[:6]:
                impact = TAG_IMPACT.get(tag)
                if impact:
                    lines.append(f"- **{tag}**：{impact}")
            lines.append("")

    lines.extend(["### 4. 对组合的影响", ""])
    for impact in regime["impacts"]:
        lines.append(f"- {impact}")
    lines.extend(
        [
            "- 以上是“环境→资产”的机械映射，不是收益预测；具体买卖仍按评分、证据分和参考区间。",
            "",
        ]
    )
    if macro.get("errors"):
        lines.append(
            "_部分宏观接口未取到："
            + "；".join(str(item) for item in macro["errors"][:3])
            + "。_"
        )
        lines.append("")
    return "\n".join(lines)
