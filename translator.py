"""Translate news titles and summaries into Russian via free Google Translate.

Uses `deep-translator` which wraps Google Translate's web endpoint — no API key
required. Set TRANSLATE_TO_RU=false in .env to disable translation entirely.
"""
from __future__ import annotations

import asyncio
import logging
import os

from deep_translator import GoogleTranslator
from deep_translator.exceptions import TranslationNotFound

log = logging.getLogger(__name__)

# Google Translate has a 5000-char limit per call.
_CHUNK_SIZE = 4500


def _enabled() -> bool:
    return os.environ.get("TRANSLATE_TO_RU", "true").strip().lower() in ("1", "true", "yes", "on")


def _translate_sync(text: str, target: str) -> str:
    if not text:
        return text
    parts: list[str] = []
    for i in range(0, len(text), _CHUNK_SIZE):
        chunk = text[i : i + _CHUNK_SIZE]
        try:
            translated = GoogleTranslator(source="auto", target=target).translate(chunk)
            parts.append(translated or chunk)
        except (TranslationNotFound, Exception):  # noqa: BLE001
            log.exception("Translation chunk failed, keeping original text")
            parts.append(chunk)
    return "".join(parts)


async def translate_to_ru(text: str) -> str:
    """Translate ``text`` into Russian. Returns the original text on failure."""
    if not _enabled() or not text:
        return text
    return await asyncio.to_thread(_translate_sync, text, "ru")
