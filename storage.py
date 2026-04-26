"""Tiny JSON-file persistence for the bot.

Two pieces of state:
- seen.json: list of news item ids we've already posted.
- config.json: per-guild channel id to post into.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DEADLOCK_BOT_DATA", "data"))
SEEN_FILE = DATA_DIR / "seen.json"
CONFIG_FILE = DATA_DIR / "config.json"

# Cap how many ids we keep around — prevents the file from growing forever.
MAX_SEEN = 500


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        with SEEN_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_seen(ids: list[str]) -> None:
    _ensure_dir()
    trimmed = ids[-MAX_SEEN:]
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)
    tmp.replace(SEEN_FILE)


def load_config() -> dict[str, int]:
    """Return mapping of guild_id (str) -> channel_id (int)."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def save_config(cfg: dict[str, int]) -> None:
    _ensure_dir()
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({str(k): int(v) for k, v in cfg.items()}, f, ensure_ascii=False, indent=2)
    tmp.replace(CONFIG_FILE)
