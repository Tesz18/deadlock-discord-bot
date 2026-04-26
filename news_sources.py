"""News fetchers for Deadlock.

Two sources:
1. Steam News API for app id 1422450 (Deadlock) — official Valve announcements.
2. forums.playdeadlock.com Changelog RSS — patch notes from the official forum.
"""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import aiohttp
import feedparser

DEADLOCK_APPID = 1422450
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
    f"?appid={DEADLOCK_APPID}&count=15&format=json"
)
FORUM_RSS_URL = "https://forums.playdeadlock.com/forums/changelog.10/index.rss"

# Used as a fallback embed image when no image is found in the news content.
DEADLOCK_HEADER_IMAGE = (
    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{DEADLOCK_APPID}/header.jpg"
)

USER_AGENT = "DeadlockNewsBot/1.0 (Discord bot)"


@dataclass
class NewsItem:
    """A single piece of news from any source."""

    source: str          # "steam" or "forum"
    item_id: str         # unique id within source, used for dedup
    title: str
    url: str
    published_ts: int    # unix seconds
    author: str
    summary: str         # plain-text excerpt, already trimmed
    image_url: str       # always set (real image or Deadlock header fallback)

    @property
    def published_dt(self) -> datetime:
        return datetime.fromtimestamp(self.published_ts, tz=timezone.utc)


_IMG_PATTERNS = (
    re.compile(r"\[img\]\s*([^\[\s]+?)\s*\[/img\]", re.IGNORECASE),
    re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE),
    re.compile(r"!\[[^\]]*\]\(([^\s)]+)\)"),
)


def _extract_image_url(*texts: str) -> str | None:
    """Pull the first plausible image URL out of any of the given strings."""
    for text in texts:
        if not text:
            continue
        for pat in _IMG_PATTERNS:
            m = pat.search(text)
            if m:
                url = html.unescape(m.group(1).strip())
                if url.startswith("http"):
                    return url
    return None


def _strip_html(text: str) -> str:
    """Remove HTML tags / Steam BBCode-ish markup and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[/?[a-zA-Z]+\]", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def fetch_steam_news(session: aiohttp.ClientSession) -> list[NewsItem]:
    async with session.get(STEAM_NEWS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items: list[NewsItem] = []
    for raw in data.get("appnews", {}).get("newsitems", []):
        gid = str(raw.get("gid") or raw.get("url") or "")
        if not gid:
            continue
        contents = raw.get("contents", "") or ""
        image_url = _extract_image_url(contents) or DEADLOCK_HEADER_IMAGE
        items.append(
            NewsItem(
                source="steam",
                item_id=f"steam:{gid}",
                title=raw.get("title", "Without title"),
                url=raw.get("url", ""),
                published_ts=int(raw.get("date", 0)),
                author=raw.get("author") or raw.get("feedlabel") or "Steam",
                summary=_truncate(_strip_html(contents)),
                image_url=image_url,
            )
        )
    return items


async def fetch_forum_rss(session: aiohttp.ClientSession) -> list[NewsItem]:
    async with session.get(FORUM_RSS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        body = await resp.text()

    parsed = await asyncio.to_thread(feedparser.parse, body)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        guid = entry.get("id") or link
        if not guid:
            continue
        if entry.get("published_parsed"):
            published_ts = int(
                datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).timestamp()
            )
        else:
            published_ts = 0
        summary_html = entry.get("summary", "") or ""
        image_url = _extract_image_url(summary_html) or DEADLOCK_HEADER_IMAGE
        items.append(
            NewsItem(
                source="forum",
                item_id=f"forum:{guid}",
                title=entry.get("title", "Without title"),
                url=link,
                published_ts=published_ts,
                author=entry.get("author", "playdeadlock.com"),
                summary=_truncate(_strip_html(summary_html)),
                image_url=image_url,
            )
        )
    return items


async def fetch_all_news() -> list[NewsItem]:
    """Fetch from every source. Failures in one source don't block the other."""
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(headers=headers) as session:
        results = await asyncio.gather(
            fetch_steam_news(session),
            fetch_forum_rss(session),
            return_exceptions=True,
        )

    items: list[NewsItem] = []
    for res in results:
        if isinstance(res, Exception):
            continue
        items.extend(res)

    items.sort(key=lambda x: x.published_ts, reverse=True)
    return items


def filter_new(items: Iterable[NewsItem], seen_ids: set[str]) -> list[NewsItem]:
    """Return items whose id is not in seen_ids, oldest first (so older posts go first)."""
    fresh = [it for it in items if it.item_id not in seen_ids]
    fresh.sort(key=lambda x: x.published_ts)
    return fresh
