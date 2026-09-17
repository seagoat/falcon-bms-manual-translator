"""Runtime configuration: config/local.json overridden by environment.

Environment variables (highest priority):
    DEEPSEEK_API_KEY, MT_BASE_URL, MT_MODEL, MT_PROVIDER, MT_DATA_DIR,
    MT_ORIGIN_DIR, MT_RENDER_DPI, MT_WEB_PORT
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("MT_CONFIG", str(ROOT / "config" / "local.json")))

DEFAULTS: dict[str, Any] = {
    "deepseek_api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    "fallback_model": "deepseek-flash",
    "provider": "deepseek",
    "concurrency": 6,
    "batch_segments": 12,
    "batch_chars": 1800,
    "request_timeout": 120,
    "max_retries": 3,
    "data_dir": "data",
    "origin_dir": "origin",
    "render_dpi": 110,
    "web_host": "127.0.0.1",
    "web_port": 8777,
}

_ENV_MAP = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "base_url": "MT_BASE_URL",
    "model": "MT_MODEL",
    "provider": "MT_PROVIDER",
    "data_dir": "MT_DATA_DIR",
    "origin_dir": "MT_ORIGIN_DIR",
    "render_dpi": "MT_RENDER_DPI",
    "web_port": "MT_WEB_PORT",
}

_cache: dict[str, Any] | None = None


def load(reload: bool = False) -> dict[str, Any]:
    """Return the merged configuration dict."""
    global _cache
    if _cache is not None and not reload:
        return _cache
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    for key, env in _ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    for key in ("concurrency", "batch_segments", "batch_chars", "request_timeout",
                "max_retries", "render_dpi", "web_port"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]
    _cache = cfg
    return cfg


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def root() -> Path:
    return ROOT


def data_dir() -> Path:
    p = ROOT / str(get("data_dir", "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "app.db"


def origin_dir() -> Path:
    return ROOT / str(get("origin_dir", "origin"))


def derived_dir(*parts: str) -> Path:
    p = data_dir() / "derived"
    for part in parts:
        p = p / str(part)
    p.mkdir(parents=True, exist_ok=True)
    return p


def pdfs_dir() -> Path:
    p = data_dir() / "pdfs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p
