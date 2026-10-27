from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from fund_tracker.config import BASE_DIR, ensure_dirs as _ensure_dirs

STOCK_CONFIG_PATH = BASE_DIR / "stock-config.json"

DEFAULTS: dict[str, Any] = {
    "data_dir": "stock-data",
    "report_dir": "stock-reports",
    "holdings_file": "stock-data/holdings.json",
    "risk_free_rate": 1.5,
    "request_timeout": 20,
    "cache_hours": 6,
    "financial_cache_hours": 24,
    "announcement_cache_hours": 6,
    "stale_days": 5,
    "max_drawdown_tolerance": 15.0,
    "min_history_days": 120,
    "alerts": {
        "daily_move_pct": 5.0,
        "dd_1y_pct": 20.0,
        "dd_3y_pct": 35.0,
        "volume_spike": 2.0,
        "market_cap_min_yi": 50.0,
        "pe_max": 80.0,
        "score_drop": 8.0,
        "announcement_days": 45,
    },
    "notify": {
        "macos": True,
        "webhook_url": "",
        "webhook_secret": "",
        "full_report": "all",
    },
    "schedule": {
        "hour": 22,
        "minute": 40,
    },
    "review": {
        "enabled": True,
        "auto": True,
        "interval_trading_days": 2,
    },
    "summary": {
        "enabled": True,
        "weekly_enabled": True,
        "monthly_enabled": True,
    },
    "macro": {
        "enabled": True,
        "news_enabled": True,
        "news_limit": 8,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | None = None) -> dict:
    path = path or STOCK_CONFIG_PATH
    if not path.exists():
        return deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, raw)


def save_config(config: dict, path: Path | None = None) -> None:
    path = path or STOCK_CONFIG_PATH
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def ensure_dirs(config: dict) -> None:
    _ensure_dirs(config)
    holdings = config.get("holdings_file")
    if holdings:
        (BASE_DIR / holdings).parent.mkdir(parents=True, exist_ok=True)
