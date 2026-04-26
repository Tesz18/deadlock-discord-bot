"""Translation helpers — Russian via free Google Translate (no API key).

Set TRANSLATE_TO_RU=false in .env to disable translation entirely.
"""
from __future__ import annotations

import asyncio
import logging
import os

from deep_translator import GoogleTranslator

log = logging.getLogger(__name__)

# Google Translate has a 5000-char limit per call.
_GT_CHUNK = 4500


def is_enabled() -> bool:
    return os.environ.get("TRANSLATE_TO_RU", "true").strip().lower() in ("1", "true", "yes", "on")


def _translate_sync(text: str, target: str) -> str:
    if not text:
        return text
    parts: list[str] = []
    for i in range(0, len(text), _GT_CHUNK):
        chunk = text[i : i + _GT_CHUNK]
        try:
            translated = GoogleTranslator(source="auto", target=target).translate(chunk)
            parts.append(translated or chunk)
        except Exception:
            log.exception("Translation chunk failed, keeping original text")
            parts.append(chunk)
    return "".join(parts)


async def translate_to_ru(text: str) -> str:
    """Translate ``text`` into Russian. Returns the original text on failure."""
    if not is_enabled() or not text:
        return text
    return await asyncio.to_thread(_translate_sync, text, "ru")


def chunk_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Split ``text`` into chunks <= ``limit`` chars, preferring sensible breakpoints."""
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = limit
        for sep in ("\n\n", "\n", ". ", " - ", " "):
            idx = remaining.rfind(sep, 0, limit)
            if idx >= limit // 2:
                cut = idx + len(sep)
                break
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
