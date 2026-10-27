from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from fund_tracker.fetch import FetchError, HttpClient, _to_float

LOG = logging.getLogger(__name__)

EASTMONEY_HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}


def is_stock_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(value).strip()))


def stock_secid(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def stock_exchange_prefix(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("8", "4")):
        return "BJ"
    return "SZ"


def search_stock(client: HttpClient, keyword: str) -> list[dict]:
    keyword = str(keyword).strip()
    if not keyword:
        return []
    url = (
        "https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={keyword}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:search:{keyword}",
            cache_hours=24,
            headers=EASTMONEY_HEADERS,
        )
    except FetchError:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = ((payload.get("QuotationCodeTable") or {}).get("Data") or [])
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("Classify") != "AStock":
            continue
        code = str(row.get("Code") or "").strip()
        if not is_stock_code(code):
            continue
        result.append(
            {
                "code": code,
                "name": str(row.get("Name") or ""),
                "secid": str(row.get("QuoteID") or stock_secid(code)),
                "market": str(row.get("SecurityTypeName") or ""),
            }
        )
    return result


def _stock_symbol(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def _parse_eastmoney_quote(code: str, text: str) -> dict | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None

    def scaled(key: str, factor: float = 100.0) -> float | None:
        value = _to_float(data.get(key))
        return None if value is None else value / factor

    name = str(data.get("f58") or code)
    price = scaled("f43")
    if price is None or price <= 0:
        return None
    return {
        "code": code,
        "name": name,
        "secid": stock_secid(code),
        "price": price,
        "high": scaled("f44"),
        "low": scaled("f45"),
        "open": scaled("f46"),
        "prev_close": scaled("f60"),
        "change": scaled("f169"),
        "pct": scaled("f170"),
        "amplitude": scaled("f171"),
        "volume": _to_float(data.get("f47")),
        "amount": _to_float(data.get("f48")),
        "volume_ratio": scaled("f50"),
        "turnover": scaled("f168"),
        "pe": scaled("f162"),
        "pb": scaled("f167"),
        "market_cap": _to_float(data.get("f116")),
        "float_market_cap": _to_float(data.get("f117")),
        "industry": str(data.get("f127") or ""),
        "region": str(data.get("f128") or ""),
        "concepts": str(data.get("f129") or ""),
        "source": "eastmoney",
    }


def _fetch_stock_quote_tencent(client: HttpClient, code: str) -> dict | None:
    symbol = _stock_symbol(code)
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        text = client.get(
            url,
            cache_key=f"stock:quote-tencent:{code}:{date.today().isoformat()}",
            cache_hours=1,
            encoding="gbk",
            headers={
                "Referer": "https://gu.qq.com/",
                "User-Agent": EASTMONEY_HEADERS["User-Agent"],
            },
        )
    except FetchError as exc:
        LOG.warning("腾讯行情获取失败 %s: %s", code, exc)
        return None
    match = re.search(r'="(.*)";', text)
    if not match:
        return None
    fields = match.group(1).split("~")
    if len(fields) < 54:
        return None

    def value(index: int, factor: float = 1.0) -> float | None:
        if index >= len(fields):
            return None
        number = _to_float(fields[index])
        return None if number is None else number / factor

    price = value(3)
    if price is None or price <= 0:
        return None
    amount = None
    if len(fields) > 35 and "/" in fields[35]:
        parts = fields[35].split("/")
        if len(parts) >= 3:
            amount = _to_float(parts[2])
    if amount is None:
        amount = value(37, 1.0)
        if amount is not None:
            amount *= 10000
    name = fields[1] if len(fields) > 1 else code
    return {
        "code": code,
        "name": name,
        "secid": stock_secid(code),
        "price": price,
        "high": value(33),
        "low": value(34),
        "open": value(5),
        "prev_close": value(4),
        "change": value(31),
        "pct": value(32),
        "amplitude": value(43),
        "volume": value(6),
        "amount": amount,
        "volume_ratio": value(49),
        "turnover": value(38),
        "pe": value(52) if value(52) is not None else value(39),
        "pb": value(46),
        "market_cap": (value(44) * 1e8) if value(44) is not None else None,
        "float_market_cap": (value(45) * 1e8) if value(45) is not None else None,
        "industry": "",
        "region": "",
        "concepts": "",
        "source": "tencent",
    }


def fetch_stock_quote(client: HttpClient, code: str) -> dict:
    code = str(code).strip()
    fallback = _fetch_stock_quote_tencent(client, code)
    if fallback:
        return fallback

    secid = stock_secid(code)
    fields = ",".join(
        [
            "f43", "f44", "f45", "f46", "f47", "f48", "f49", "f50",
            "f51", "f52", "f57", "f58", "f60", "f116", "f117", "f162",
            "f167", "f168", "f169", "f170", "f171", "f127", "f128", "f129",
        ]
    )
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields={fields}"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:quote:{code}:{date.today().isoformat()}",
            cache_hours=1,
            headers=EASTMONEY_HEADERS,
        )
        parsed = _parse_eastmoney_quote(code, text)
        if parsed:
            return parsed
    except FetchError as exc:
        LOG.warning("东财行情获取失败 %s: %s", code, exc)
    return {"code": code, "name": "", "quote_error": "行情接口不可用"}


def _parse_eastmoney_kline(text: str) -> list[dict]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    klines = (data or {}).get("klines") if isinstance(data, dict) else None
    if not isinstance(klines, list):
        return []
    rows: list[dict] = []
    for line in klines:
        cells = str(line).split(",")
        if len(cells) < 11:
            continue
        try:
            row = {
                "date": cells[0],
                "open": float(cells[1]),
                "close": float(cells[2]),
                "high": float(cells[3]),
                "low": float(cells[4]),
                "volume": float(cells[5]),
                "amount": float(cells[6]),
                "amplitude": float(cells[7]),
                "pct": float(cells[8]),
                "change": float(cells[9]),
                "turnover": float(cells[10]),
            }
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def _fetch_stock_kline_sina(
    client: HttpClient,
    code: str,
    *,
    years: int,
) -> list[dict]:
    symbol = _stock_symbol(code)
    datalen = max(300, int(years * 420))
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:kline-sina:{code}:{date.today().isoformat()}",
            cache_hours=6,
            headers={
                "Referer": "https://finance.sina.com.cn/",
                "User-Agent": EASTMONEY_HEADERS["User-Agent"],
            },
        )
    except FetchError as exc:
        LOG.warning("新浪 K 线获取失败 %s: %s", code, exc)
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    rows: list[dict] = []
    previous_close: float | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            close = float(item.get("close"))
            high = float(item.get("high"))
            low = float(item.get("low"))
            open_price = float(item.get("open"))
            volume = float(item.get("volume"))
        except (TypeError, ValueError):
            continue
        pct = (
            (close / previous_close - 1) * 100
            if previous_close and previous_close > 0
            else None
        )
        amplitude = (
            (high - low) / previous_close * 100
            if previous_close and previous_close > 0
            else None
        )
        rows.append(
            {
                "date": str(item.get("day") or ""),
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": None,
                "amplitude": amplitude,
                "pct": pct,
                "change": None,
                "turnover": None,
            }
        )
        previous_close = close
    return rows


def _fetch_stock_kline_tencent(
    client: HttpClient,
    code: str,
    *,
    years: int,
) -> list[dict]:
    symbol = _stock_symbol(code)
    count = max(300, int(years * 420))
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{count},qfq"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:kline-tencent:{code}:{date.today().isoformat()}",
            cache_hours=6,
            headers={
                "Referer": "https://gu.qq.com/",
                "User-Agent": EASTMONEY_HEADERS["User-Agent"],
            },
        )
    except FetchError as exc:
        LOG.warning("腾讯 K 线获取失败 %s: %s", code, exc)
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    data = (payload.get("data") or {}).get(symbol) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    items = data.get("qfqday") or data.get("day") or []
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    previous_close: float | None = None
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        try:
            day = str(item[0])
            open_price = float(item[1])
            close = float(item[2])
            high = float(item[3])
            low = float(item[4])
            volume = float(item[5])
        except (TypeError, ValueError):
            continue
        pct = (
            (close / previous_close - 1) * 100
            if previous_close and previous_close > 0
            else None
        )
        rows.append(
            {
                "date": day,
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": None,
                "amplitude": None,
                "pct": pct,
                "change": None,
                "turnover": None,
            }
        )
        previous_close = close
    return rows


def fetch_stock_kline(
    client: HttpClient,
    code: str,
    *,
    years: int = 4,
) -> list[dict]:
    code = str(code).strip()
    rows = _fetch_stock_kline_sina(client, code, years=years)
    if len(rows) >= 2:
        rows.sort(key=lambda item: item["date"])
        return rows

    secid = stock_secid(code)
    begin = (date.today() - timedelta(days=max(365, years * 370))).strftime("%Y%m%d")
    end = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&klt=101&fqt=1&beg={begin}&end={end}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:kline:{code}:{date.today().isoformat()}",
            cache_hours=6,
            headers=EASTMONEY_HEADERS,
        )
        rows = _parse_eastmoney_kline(text)
        if len(rows) >= 2:
            rows.sort(key=lambda item: item["date"])
            return rows
    except FetchError as exc:
        LOG.warning("东财 K 线获取失败 %s: %s", code, exc)

    rows = _fetch_stock_kline_tencent(client, code, years=years)
    rows.sort(key=lambda item: item["date"])
    return rows


def fetch_stock_profile(client: HttpClient, code: str) -> dict:
    code = str(code).strip()
    prefix = stock_exchange_prefix(code)
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/"
        f"CompanySurveyAjax?code={prefix}{code}"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:profile:{code}",
            cache_hours=168,
            headers={
                **EASTMONEY_HEADERS,
                "Referer": "https://emweb.securities.eastmoney.com/",
            },
        )
    except FetchError as exc:
        LOG.warning("公司概况获取失败 %s: %s", code, exc)
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    data = payload.get("jbzl") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    return {
        "full_name": str(data.get("gsmc") or ""),
        "industry": str(data.get("sshy") or data.get("sszjhhy") or ""),
        "region": str(data.get("qy") or ""),
        "business": str(data.get("jyfw") or data.get("zyyw") or ""),
        "website": str(data.get("gswz") or ""),
    }


def fetch_stock_financials(client: HttpClient, code: str) -> dict:
    code = str(code).strip()
    prefix = stock_exchange_prefix(code)
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
        f"ZYZBAjaxNew?type=0&code={prefix}{code}"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:financial:{code}",
            cache_hours=24,
            headers={
                **EASTMONEY_HEADERS,
                "Referer": "https://emweb.securities.eastmoney.com/",
            },
        )
    except FetchError as exc:
        LOG.warning("股票财务数据获取失败 %s: %s", code, exc)
        return {"error": str(exc)}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "财务 JSON 解析失败"}
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return {"error": "财务数据为空"}
    latest = next((item for item in items if isinstance(item, dict)), None)
    if not latest:
        return {"error": "财务数据格式异常"}

    def number(key: str) -> float | None:
        value = latest.get(key)
        if value in (None, "", "--"):
            return None
        return _to_float(value)

    return {
        "report_date": str(latest.get("REPORT_DATE") or "")[:10],
        "report_type": str(latest.get("REPORT_TYPE") or ""),
        "report_name": str(latest.get("REPORT_DATE_NAME") or ""),
        "notice_date": str(latest.get("NOTICE_DATE") or "")[:10],
        "roe": number("ROEJQ"),
        "revenue": number("TOTALOPERATEREVE"),
        "net_profit": number("PARENTNETPROFIT"),
        "revenue_growth": number("TOTALOPERATEREVETZ"),
        "profit_growth": number("PARENTNETPROFITTZ"),
        "gross_margin": number("XSMLL"),
        "net_margin": number("XSJLL"),
        "debt_ratio": number("ZCFZL"),
        "cash_ratio": number("JYXJLYYSR"),
        "eps": number("EPSJB"),
        "bps": number("BPS"),
    }


def fetch_stock_announcements(
    client: HttpClient,
    code: str,
    *,
    limit: int = 20,
) -> list[dict]:
    code = str(code).strip()
    url = (
        "https://np-anotice-stock.eastmoney.com/api/security/ann"
        f"?sr=-1&page_size={max(1, limit)}&page_index=1&ann_type=A"
        f"&client_source=web&stock_list={code}"
    )
    try:
        text = client.get(
            url,
            cache_key=f"stock:ann:{code}:{limit}",
            cache_hours=6,
            headers={
                **EASTMONEY_HEADERS,
                "Referer": "https://data.eastmoney.com/",
            },
        )
    except FetchError as exc:
        LOG.warning("股票公告获取失败 %s: %s", code, exc)
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = ((payload.get("data") or {}).get("list") or [])
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        art_code = str(row.get("art_code") or "")
        if not art_code:
            continue
        columns = row.get("columns") or []
        column_names = [
            str(item.get("column_name") or "")
            for item in columns
            if isinstance(item, dict)
        ]
        result.append(
            {
                "code": code,
                "title": str(row.get("title") or row.get("title_ch") or ""),
                "date": str(row.get("notice_date") or row.get("display_time") or "")[:10],
                "art_code": art_code,
                "url": f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html",
                "columns": column_names,
            }
        )
    return result


def fetch_stock(
    client: HttpClient,
    code: str,
    *,
    years: int = 4,
    announcement_limit: int = 20,
) -> dict:
    code = str(code).strip()
    quote = fetch_stock_quote(client, code)
    kline = fetch_stock_kline(client, code, years=years)
    profile = fetch_stock_profile(client, code)
    financials = fetch_stock_financials(client, code)
    announcements = fetch_stock_announcements(
        client, code, limit=announcement_limit
    )
    if isinstance(quote, dict):
        if not quote.get("industry") and profile.get("industry"):
            quote["industry"] = profile["industry"]
        if not quote.get("region") and profile.get("region"):
            quote["region"] = profile["region"]
    quote_name = str((quote or {}).get("name") or "").strip()
    name = quote_name or profile.get("full_name") or code
    return {
        "code": code,
        "name": name,
        "quote": quote,
        "kline": kline,
        "profile": profile,
        "financials": financials,
        "announcements": announcements,
    }
