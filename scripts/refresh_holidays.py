#!/usr/bin/env python3
"""Refresh the bundled A-share holiday/trading-day dataset.

Derives the complete trading-day set from the SSE Composite index
(eastmoney push2his daily klines) — every published bar is a trading day,
so holidays are derivable weekdays WITHOUT that bar. No guessing of
official holiday schedules.

Manual operation (not in any launchd path):
    python3 scripts/refresh_holidays.py            # 2024-01-01 .. today
    python3 scripts/refresh_holidays.py --beg 2020-01-01

Writes shared/data/ashare_holidays.json and prints the diff summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

DATA_PATH = BASE / "shared/data/ashare_holidays.json"
KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.000001&fields1=f1,f2,f3&fields2=f51"
    "&klt=101&fqt=1&beg={beg}&end=20991231&lmt=100000"
)


def fetch_trading_days(beg: str) -> list[str]:
    import requests

    response = requests.get(KLINE_URL.format(beg=beg), timeout=30)
    response.raise_for_status()
    lines = (response.json().get("data") or {}).get("klines") or []
    days = [line.split(",", 1)[0] for line in lines]
    if not days:
        raise RuntimeError("klines 为空：数据源返回异常，先重试或检查网络")
    return days


def _load_existing() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def derive_holidays(trading_days: list[str]) -> list[str]:
    """Weekdays inside coverage WITHOUT a published trading bar."""
    settled = set(trading_days)
    holidays: list[str] = []
    cursor = date.fromisoformat(trading_days[0])
    end = date.fromisoformat(trading_days[-1])
    while cursor <= end:
        text = cursor.isoformat()
        if cursor.weekday() < 5 and text not in settled:
            holidays.append(text)
        cursor += timedelta(days=1)
    return holidays


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新 A 股节假日/交易日数据集")
    parser.add_argument("--beg", default="2024-01-01", help="回溯起始日期（默认 2024-01-01）")
    args = parser.parse_args()

    beg = args.beg.replace("-", "")
    new_days = fetch_trading_days(beg)
    old = _load_existing()
    old_days = set(old.get("trading_days") or [])

    added = [day for day in new_days if day not in old_days]
    removed = [day for day in old_days if day not in new_days]

    payload = {
        "source": "eastmoney push2his secid=1.000001 daily klines（上证综指，bar=交易日）",
        "updated": date.today().isoformat(),
        "coverage_start": new_days[0],
        "coverage_end": new_days[-1],
        "holidays": derive_holidays(new_days),
        "trading_days": new_days,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DATA_PATH)

    print(
        f"交易日 {new_days[0]} … {new_days[-1]}. 共 {len(new_days)} 天；"
        f"节假日（工作日休市）{len(payload['holidays'])} 天。"
    )
    if added:
        print(f"新增 {len(added)} 个交易日：{added[:5]}{'…' if len(added) > 5 else ''}")
    if removed:
        print(f"移除 {len(removed)} 个（原数据里现在无 bar）：{removed[:5]}")
    if not added and not removed:
        print("与现有数据一致，无变化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
