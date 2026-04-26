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

    @property
    def published_dt(self) -> datetime:
        return datetime.fromtimestamp(self.published_ts, tz=timezone.utc)


def _strip_html(text: str) -> str:
    """Remove HTML tags / Steam BBCode-ish markup and collapse whitespace."""
    if not text:
        return ""
    # Strip tags like <p>, <br/>, [p], [b], [/b] etc.
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
        items.append(
            NewsItem(
                source="steam",
                item_id=f"steam:{gid}",
                title=raw.get("title", "Without title"),
                url=raw.get("url", ""),
                published_ts=int(raw.get("date", 0)),
                author=raw.get("author") or raw.get("feedlabel") or "Steam",
                summary=_truncate(_strip_html(raw.get("contents", ""))),
            )
        )
    return items


async def fetch_forum_rss(session: aiohttp.ClientSession) -> list[NewsItem]:
    async with session.get(FORUM_RSS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        body = await resp.text()

    # feedparser is sync but cheap on a small RSS; run in thread to avoid blocking.
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
        items.append(
            NewsItem(
                source="forum",
                item_id=f"forum:{guid}",
                title=entry.get("title", "Without title"),
                url=link,
                published_ts=published_ts,
                author=entry.get("author", "playdeadlock.com"),
                summary=_truncate(_strip_html(entry.get("summary", ""))),
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
            # Logged by caller; just skip.
            continue
        items.extend(res)

    # Sort newest first.
    items.sort(key=lambda x: x.published_ts, reverse=True)
    return items


def filter_new(items: Iterable[NewsItem], seen_ids: set[str]) -> list[NewsItem]:
    """Return items whose id is not in seen_ids, oldest first (so older posts go first)."""
    fresh = [it for it in items if it.item_id not in seen_ids]
    fresh.sort(key=lambda x: x.published_ts)  # oldest first when posting
    return fresh
