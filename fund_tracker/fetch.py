from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import BASE_DIR, ensure_dirs, load_config

LOG = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class FetchError(RuntimeError):
    pass


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text in {"--", "-", "暂无", "nan", "None"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else default


class HttpClient:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or load_config()
        ensure_dirs(self.config)
        self.cache_dir = BASE_DIR / self.config["data_dir"] / "cache"
        self.timeout = int(self.config.get("request_timeout", 20))
        self.default_cache_hours = float(self.config.get("cache_hours", 6))
        self.force = False
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.last_error: str | None = None

    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.txt", self.cache_dir / f"{digest}.json"

    def _read_cache(self, key: str) -> tuple[str | None, float | None]:
        text_path, meta_path = self._cache_paths(key)
        if not text_path.exists():
            return None, None
        try:
            age_hours = (
                time.time() - text_path.stat().st_mtime
            ) / 3600.0
            return text_path.read_text(encoding="utf-8"), age_hours
        except OSError:
            return None, None

    def _write_cache(self, key: str, text: str, url: str) -> None:
        text_path, meta_path = self._cache_paths(key)
        try:
            text_path.write_text(text, encoding="utf-8")
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            LOG.warning("无法写入缓存: %s", text_path)

    def _request(
        self,
        method: str,
        url: str,
        *,
        cache_key: str,
        cache_hours: float | None = None,
        encoding: str = "utf-8-sig",
        headers: dict | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
    ) -> str:
        cache_hours = (
            self.default_cache_hours if cache_hours is None else cache_hours
        )
        cached, age = self._read_cache(cache_key)
        stale = cached
        if self.force:
            cached = None
            age = None
        if cached is not None and age is not None and age <= cache_hours:
            return cached

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json_body,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                text = response.content.decode(encoding, errors="replace")
                if not text.strip():
                    raise FetchError(f"空响应: {url}")
                self._write_cache(cache_key, text, url)
                return text
            except Exception as exc:  # noqa: BLE001 - network can fail many ways
                last_exc = exc
                LOG.warning("请求失败(%s/3): %s -> %s", attempt + 1, url, exc)
                time.sleep(1.0 + attempt)

        if stale is not None and not self.force:
            LOG.warning("使用过期缓存: %s", cache_key)
            return stale
        raise FetchError(f"请求失败: {url} ({last_exc})") from last_exc

    def get(
        self,
        url: str,
        *,
        cache_key: str,
        cache_hours: float | None = None,
        encoding: str = "utf-8-sig",
        headers: dict | None = None,
    ) -> str:
        return self._request(
            "GET",
            url,
            cache_key=cache_key,
            cache_hours=cache_hours,
            encoding=encoding,
            headers=headers,
        )

    def post(
        self,
        url: str,
        *,
        cache_key: str,
        cache_hours: float | None = None,
        encoding: str = "utf-8-sig",
        headers: dict | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
    ) -> str:
        return self._request(
            "POST",
            url,
            cache_key=cache_key,
            cache_hours=cache_hours,
            encoding=encoding,
            headers=headers,
            data=data,
            json_body=json_body,
        )


def parse_js_var(text: str, name: str) -> Any:
    pattern = r"var\s+%s\s*=\s*(.*?);" % re.escape(name)
    match = re.search(pattern, text, re.S)
    if not match:
        return None
    raw = match.group(1).strip()
    if raw in {"", "null", "undefined"}:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw.strip('"').strip("'")


def fetch_fund(client: HttpClient, code: str) -> dict:
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise FetchError(f"基金代码必须是 6 位数字: {code}")
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    text = client.get(
        url,
        cache_key=f"pingzhong:{code}",
        cache_hours=client.default_cache_hours,
    )
    name = parse_js_var(text, "fS_name")
    if not name:
        raise FetchError(f"未找到基金 {code}，请检查代码")
    return {
        "code": code,
        "name": str(name).strip(),
        "source_rate": parse_js_var(text, "fund_sourceRate"),
        "current_rate": parse_js_var(text, "fund_Rate"),
        "net_worth": parse_js_var(text, "Data_netWorthTrend") or [],
        "ac_worth": parse_js_var(text, "Data_ACWorthTrend") or [],
        "scale": parse_js_var(text, "Data_fluctuationScale"),
        "allocation": parse_js_var(text, "Data_assetAllocation"),
        "managers": parse_js_var(text, "Data_currentFundManager") or [],
        "rank_history": parse_js_var(text, "Data_rateInSimilarType") or [],
        "evaluation": parse_js_var(text, "Data_performanceEvaluation"),
        "buy_sedemption": parse_js_var(text, "Data_buySedemption"),
    }


def fetch_profile(client: HttpClient, code: str) -> dict:
    url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    text = client.get(
        url,
        cache_key=f"profile:{code}",
        cache_hours=float(client.config.get("profile_cache_hours", 168)),
        encoding="utf-8",
    )

    def find(pattern: str) -> str | None:
        match = re.search(pattern, text, re.S)
        return match.group(1).strip() if match else None

    buy_fee = None
    discount = re.search(r"天天基金优惠费率：.*?([\d.]+)%", text, re.S)
    if discount:
        buy_fee = _to_float(discount.group(1))
    if buy_fee is None:
        buy_fee = _to_float(find(r"最高申购费率</th><td>([\d.]+)%"))

    return {
        "fund_type": find(r"基金类型</th><td>(.*?)</td>"),
        "manage_fee": _to_float(find(r"管理费率</th><td>([\d.]+)%")),
        "custody_fee": _to_float(find(r"托管费率</th><td>([\d.]+)%")),
        "service_fee": _to_float(find(r"销售服务费率</th><td>([\d.]+)%")),
        "buy_fee_max": _to_float(find(r"最高申购费率</th><td>([\d.]+)%")),
        "buy_fee": buy_fee,
        "redeem_fee_max": _to_float(find(r"最高赎回费率</th><td>([\d.]+)%")),
        "net_assets_yi": _to_float(find(r"净资产规模</th><td>([\d.]+)亿元")),
        "inception": find(r"成立日期/规模</th><td>(\d{4}年\d{2}月\d{2}日)"),
        "benchmark": find(r"业绩比较基准</th><td[^>]*>(.*?)</td>"),
    }


def fetch_ranking(
    client: HttpClient,
    ft: str = "zq",
    *,
    pages: int = 1,
    page_size: int = 50,
    sort: str = "3nzf",
    subtype: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict]:
    end = end or date.today()
    start = start or (end - timedelta(days=365 * 3 + 30))
    records: list[dict] = []
    total_pages = pages
    qdii_param = f"{subtype}|" if subtype else ""
    subtype_param = f"{subtype},,,,," if subtype else ",,,,,"

    for page in range(1, pages + 1):
        url = (
            "https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf"
            f"&ft={quote(ft)}&rs=&gs=0&sc={quote(sort)}&st=desc"
            f"&sd={start.isoformat()}&ed={end.isoformat()}"
            f"&qdii={quote(qdii_param)}&tabSubtype={quote(subtype_param)}"
            f"&pi={page}&pn={page_size}&dx=1"
        )
        text = client.get(
            url,
            cache_key=f"rank:{ft}:{subtype}:{sort}:{start}:{end}:{page}:{page_size}",
            cache_hours=client.default_cache_hours,
            headers={"Referer": "https://fund.eastmoney.com/data/fundranking.html"},
        )
        match = re.search(r"datas:\[(.*?)\],allRecords", text, re.S)
        if not match:
            raise FetchError(f"排行榜解析失败: ft={ft}")
        all_pages_match = re.search(r"allPages:(\d+)", text)
        if all_pages_match:
            total_pages = int(all_pages_match.group(1))

        raw_rows = re.findall(r'"([^"]*)"', match.group(1))
        for raw in raw_rows:
            fields = raw.split(",")
            if len(fields) < 19:
                continue
            rec = {
                "code": fields[0],
                "name": fields[1],
                "nav_date": fields[3],
                "unit_nav": _to_float(fields[4]),
                "acc_nav": _to_float(fields[5]),
                "daily_pct": _to_float(fields[6]),
                "week_pct": _to_float(fields[7]),
                "month1_pct": _to_float(fields[8]),
                "month3_pct": _to_float(fields[9]),
                "month6_pct": _to_float(fields[10]),
                "year1_pct": _to_float(fields[11]),
                "year2_pct": _to_float(fields[12]),
                "year3_pct": _to_float(fields[13]),
                "ytd_pct": _to_float(fields[14]),
                "since_pct": _to_float(fields[15]),
                "inception": fields[16],
                "scale_yi": _to_float(fields[18]),
                "buy_fee_pct": _to_float(fields[19]),
                "redeem_fee_pct": _to_float(fields[20]),
            }
            records.append(rec)

        if page >= total_pages:
            break
        time.sleep(0.2)

    return records


SINA_SYMBOLS = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sz399006": "创业板指",
    "sh000015": "上证红利",
}

INDEX_SECIDS = {
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "创业板指": "0.399006",
    "红利指数": "1.000015",
}

SINA_INDEX_SYMBOLS = {
    "1.000300": "sh000300",
    "1.000905": "sh000905",
    "0.399006": "sz399006",
    "1.000015": "sh000015",
}


def fetch_index_history(
    client: HttpClient,
    secid: str,
    *,
    days: int = 30,
) -> list[dict]:
    symbol = SINA_INDEX_SYMBOLS.get(secid)
    if not symbol:
        return []
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={max(60, days * 2)}"
    )
    text = client.get(
        url,
        cache_key=f"indexhist-sina:{symbol}:{date.today().isoformat()}",
        cache_hours=1,
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows: list[dict] = []
    previous_close: float | None = None
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        try:
            close = float(item.get("close"))
        except (TypeError, ValueError):
            continue
        pct = (close / previous_close - 1) * 100 if previous_close else 0.0
        rows.append(
            {
                "date": item.get("day"),
                "open": _to_float(item.get("open")),
                "close": close,
                "high": _to_float(item.get("high")),
                "low": _to_float(item.get("low")),
                "pct": round(pct, 2),
            }
        )
        previous_close = close
    return rows


def fetch_market_history(client: HttpClient, count: int = 6) -> list[dict]:
    """Fetch index changes over ``count - 1`` trading-day intervals."""
    result: list[dict] = []
    window_days = max(1, count - 1)
    for name, secid in INDEX_SECIDS.items():
        try:
            rows = fetch_index_history(client, secid, days=max(20, count * 3))
        except Exception as exc:  # noqa: BLE001 - market history is optional
            LOG.warning("指数历史获取失败 %s: %s", name, exc)
            continue
        if len(rows) < count:
            continue
        latest = rows[-1]
        base = rows[-count]
        change = (latest["close"] / base["close"] - 1) * 100 if base["close"] else None
        result.append(
            {
                "name": name,
                "current": latest["close"],
                "pct": latest["pct"],
                "pct_window": change,
                "window_days": window_days,
                "date": latest["date"],
            }
        )
    return result


def fetch_market_history_range(
    client: HttpClient,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Fetch index changes between two dates, using the first/last close in range."""
    result: list[dict] = []
    for name, secid in INDEX_SECIDS.items():
        try:
            rows = fetch_index_history(client, secid, days=120)
        except Exception as exc:  # noqa: BLE001 - market history is optional
            LOG.warning("指数区间历史获取失败 %s: %s", name, exc)
            continue
        window = [
            row
            for row in rows
            if str(row.get("date") or "") >= start_date
            and str(row.get("date") or "") <= end_date
        ]
        if len(window) < 2:
            continue
        latest = window[-1]
        base = window[0]
        change = (
            (latest["close"] / base["close"] - 1) * 100
            if base.get("close")
            else None
        )
        result.append(
            {
                "name": name,
                "current": latest["close"],
                "pct": latest["pct"],
                "pct_window": change,
                "window_days": len(window) - 1,
                "start_date": base.get("date"),
                "date": latest.get("date"),
            }
        )
    return result


def fetch_market(client: HttpClient) -> list[dict]:
    url = "https://hq.sinajs.cn/list=" + ",".join(SINA_SYMBOLS)
    try:
        text = client.get(
            url,
            cache_key="market:sina",
            cache_hours=1,
            encoding="gbk",
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
    except FetchError:
        return []

    rows: list[dict] = []
    for line in text.splitlines():
        match = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if not match:
            continue
        symbol, payload = match.group(1), match.group(2)
        fields = payload.split(",")
        if len(fields) < 6 or not fields[0]:
            continue
        current = _to_float(fields[3])
        prev_close = _to_float(fields[2])
        if current is None or not prev_close:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": fields[0] or SINA_SYMBOLS.get(symbol, symbol),
                "current": current,
                "prev_close": prev_close,
                "pct": (current / prev_close - 1) * 100,
                "date": fields[30] if len(fields) > 30 else "",
                "time": fields[31] if len(fields) > 31 else "",
            }
        )
    return rows


def fetch_realtime(client: HttpClient, code: str) -> dict | None:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    try:
        text = client.get(
            url,
            cache_key=f"realtime:{code}:{datetime.now():%Y%m%d%H}",
            cache_hours=0.25,
            headers={"Referer": "https://fund.eastmoney.com/"},
        )
    except FetchError:
        return None
    match = re.search(r"jsonpgz\((.*)\);?", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def search_fund(client: HttpClient, keyword: str) -> list[dict]:
    url = (
        "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        f"?m=1&key={quote(keyword)}"
    )
    text = client.get(
        url,
        cache_key=f"search:{keyword}",
        cache_hours=24,
        headers={"Referer": "https://fund.eastmoney.com/"},
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    result = []
    for item in payload.get("Datas", []) or []:
        code = item.get("CODE") or item.get("FundBaseInfo", {}).get("FCODE")
        name = item.get("NAME") or item.get("FundBaseInfo", {}).get("SHORTNAME")
        if code and name:
            result.append({"code": str(code), "name": str(name)})
    return result


def _clean_html_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_holding_table(text: str, kind: str) -> dict:
    report_date = None
    date_match = re.search(
        r"截止至：<font[^>]*>(\d{4}-\d{2}-\d{2})</font>",
        text,
    )
    if date_match:
        report_date = date_match.group(1)
    title_match = re.search(r"<h4 class='t'.*?</h4>", text, re.S)
    title = _clean_html_text(title_match.group(0)) if title_match else ""
    table_match = re.search(r"<table[^>]*>.*?</table>", text, re.S)
    if not table_match:
        return {"items": [], "report_date": report_date, "title": title}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match.group(0), re.S)
    items: list[dict] = []
    for row in rows[1:]:
        cells = [
            _clean_html_text(cell)
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) < 4:
            continue
        if kind == "stock" and len(cells) >= 9:
            items.append(
                {
                    "code": cells[1],
                    "name": cells[2],
                    "weight_pct": _to_float(cells[6]),
                    "market_value_wan": _to_float(cells[8]),
                }
            )
        elif kind == "bond" and len(cells) >= 5:
            items.append(
                {
                    "code": cells[1],
                    "name": cells[2],
                    "weight_pct": _to_float(cells[3]),
                    "market_value_wan": _to_float(cells[4]),
                }
            )
    return {"items": items, "report_date": report_date, "title": title}


def fetch_fund_holdings(
    client: HttpClient,
    code: str,
    *,
    cache_hours: float = 24,
) -> dict:
    """Latest disclosed stock and bond holdings from Eastmoney F10."""
    code = str(code).strip()
    year = date.today().year
    month = date.today().month
    result: dict = {
        "stock": [],
        "bond": [],
        "report_date": None,
        "stock_title": "",
        "bond_title": "",
    }
    for kind, data_type in (("stock", "jjcc"), ("bond", "zqcc")):
        url = (
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            f"?type={data_type}&code={code}&topline=10"
            f"&year={year}&month={month}&rt=0.1"
        )
        try:
            text = client.get(
                url,
                cache_key=f"holdings:{code}:{data_type}:{year}:{month}",
                cache_hours=cache_hours,
                headers={"Referer": "https://fundf10.eastmoney.com/"},
            )
        except FetchError:
            continue
        parsed = _parse_holding_table(text, kind)
        result[kind] = parsed["items"]
        if parsed.get("report_date"):
            result["report_date"] = parsed["report_date"]
        result[f"{kind}_title"] = parsed.get("title", "")
    result["top_stock_weight"] = round(
        sum(item.get("weight_pct") or 0 for item in result["stock"]),
        2,
    )
    result["top_bond_weight"] = round(
        sum(item.get("weight_pct") or 0 for item in result["bond"]),
        2,
    )
    return result


def fetch_stock_quotes(client: HttpClient, codes: list[str]) -> dict[str, dict]:
    codes = [
        str(code).strip()
        for code in codes
        if re.fullmatch(r"\d{6}", str(code).strip())
    ]
    if not codes:
        return {}

    def sina_symbol(code: str) -> str:
        if code.startswith(("6", "9")):
            return f"sh{code}"
        if code.startswith(("8", "4")):
            return f"bj{code}"
        return f"sz{code}"

    symbols = [sina_symbol(code) for code in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    try:
        text = client.get(
            url,
            cache_key=f"stockquotes:{','.join(codes)}",
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
        fields = payload.split(",")
        if len(fields) < 4 or not fields[0]:
            continue
        code = symbol[-6:]
        current = _to_float(fields[3])
        prev_close = _to_float(fields[2])
        result[code] = {
            "name": fields[0],
            "price": current,
            "pct": (
                round((current / prev_close - 1) * 100, 2)
                if current is not None and prev_close
                else None
            ),
            "pe": None,
            "pb": None,
        }
    return result


def fetch_fund_announcements(
    client: HttpClient,
    code: str,
    *,
    limit: int = 30,
    cache_hours: float = 24,
) -> list[dict]:
    code = str(code).strip()
    url = (
        "https://api.fund.eastmoney.com/f10/JJGG"
        f"?fundcode={code}&pageIndex=1&pageSize={limit}&type=0"
    )
    try:
        text = client.get(
            url,
            cache_key=f"announcements:{code}:{limit}",
            cache_hours=cache_hours,
            headers={"Referer": "https://fundf10.eastmoney.com/"},
        )
        payload = json.loads(text)
    except (FetchError, json.JSONDecodeError):
        return []
    result: list[dict] = []
    for item in payload.get("Data") or []:
        title = str(item.get("TITLE") or "").strip()
        if not title:
            continue
        announcement_id = str(item.get("ID") or "").strip()
        result.append(
            {
                "title": title,
                "date": str(item.get("PUBLISHDATEDesc") or "")[:10],
                "category": str(item.get("NEWCATEGORY") or ""),
                "id": announcement_id,
                "url": (
                    f"https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"
                    if announcement_id
                    else ""
                ),
            }
        )
    return result
