from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "data_dir": "data",
    "report_dir": "reports",
    "holdings_file": "data/holdings.json",
    "risk_free_rate": 1.5,
    "request_timeout": 20,
    "cache_hours": 6,
    "profile_cache_hours": 168,
    "extras_cache_hours": 24,
    "stale_days": 5,
    "max_drawdown_tolerance": 15.0,
    "alerts": {
        "daily_move_pct": 2.0,
        "dd_1y_pct": 8.0,
        "dd_3y_pct": 15.0,
        "score_drop": 8.0,
        "size_min_yi": 2.0,
        "size_max_yi": 200.0,
        "manager_change": True,
        "rank_pct_worse": 50.0,
        "bond_daily_move_suspicious": 5.0,
    },
    "notify": {
        "macos": True,
        "webhook_url": "",
        "webhook_secret": "",
        "full_report": "review",
    },
    "schedule": {
        "hour": 22,
        "minute": 30,
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
    path = path or CONFIG_PATH
    if not path.exists():
        return deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, raw)


def save_config(config: dict, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_dirs(config: dict) -> None:
    (BASE_DIR / config["data_dir"]).mkdir(parents=True, exist_ok=True)
    (BASE_DIR / config["data_dir"] / "cache").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / config["report_dir"]).mkdir(parents=True, exist_ok=True)
