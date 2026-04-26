"""News fetchers for Deadlock.

Two sources:
1. Steam News API for app id 1422450 (Deadlock) — official Valve announcements.
2. forums.playdeadlock.com Changelog RSS — patch notes from the official forum.

For every item we resolve:
- A unique image URL when the source provides one; otherwise we deterministically
  pick from a rotation of Steam screenshots so every item gets a different image.
- The full post body (used for translating into Russian and posting in a thread).
  The forum RSS only ships a short excerpt, so we additionally fetch the thread
  page and extract the first message body.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import aiohttp
import feedparser

log = logging.getLogger(__name__)

DEADLOCK_APPID = 1422450
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
    f"?appid={DEADLOCK_APPID}&count=15&format=json"
)
STEAM_APPDETAILS_URL = (
    f"https://store.steampowered.com/api/appdetails?appids={DEADLOCK_APPID}"
)
FORUM_RSS_URL = "https://forums.playdeadlock.com/forums/changelog.10/index.rss"

DEADLOCK_HEADER_IMAGE = (
    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{DEADLOCK_APPID}/header.jpg"
)

USER_AGENT = "DeadlockNewsBot/1.0 (Discord bot)"

_screenshot_cache: list[str] | None = None


@dataclass
class NewsItem:
    """A single piece of news from any source."""

    source: str          # "steam" or "forum"
    item_id: str         # unique id within source, used for dedup and image rotation
    title: str
    url: str
    published_ts: int    # unix seconds
    author: str
    summary: str         # plain-text excerpt, ~400 chars, used for embed description
    body_full: str       # plain-text full content (or as much as we could extract)
    image_url: str       # always set

    @property
    def published_dt(self) -> datetime:
        return datetime.fromtimestamp(self.published_ts, tz=timezone.utc)


_IMG_PATTERNS = (
    re.compile(r"\[img\]\s*([^\[\s]+?)\s*\[/img\]", re.IGNORECASE),
    re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE),
    re.compile(r"!\[[^\]]*\]\(([^\s)]+)\)"),
)


def _extract_image_url(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        for pat in _IMG_PATTERNS:
            m = pat.search(text)
            if m:
                url = html.unescape(m.group(1).strip())
                if url.startswith("http") and not _is_decorative_image(url):
                    return url
    return None


def _is_decorative_image(url: str) -> bool:
    """Filter out tiny icons, avatars, emojis that aren't content images."""
    bad_substrings = (
        "joypixels",
        "/avatars/",
        "/emoji/",
        "/icon.png",
        "logo_alternate",
        "logo_default",
    )
    return any(s in url for s in bad_substrings)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[/?[a-zA-Z]+(?:=[^\]]*)?\]", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _truncate(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def _fetch_steam_screenshots(session: aiohttp.ClientSession) -> list[str]:
    global _screenshot_cache
    if _screenshot_cache is not None:
        return _screenshot_cache
    try:
        async with session.get(
            STEAM_APPDETAILS_URL, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                _screenshot_cache = []
                return _screenshot_cache
            data = await resp.json(content_type=None)
        app = (data.get(str(DEADLOCK_APPID)) or {}).get("data", {})
        shots = [s.get("path_full") for s in app.get("screenshots", []) if s.get("path_full")]
        _screenshot_cache = shots
        log.info("Loaded %d Deadlock screenshots from Steam", len(shots))
        return shots
    except Exception:
        log.exception("Failed to fetch Steam screenshots")
        _screenshot_cache = []
        return _screenshot_cache


def _pick_rotating_image(item_id: str, screenshots: list[str]) -> str:
    pool = [DEADLOCK_HEADER_IMAGE, *screenshots]
    h = int(hashlib.md5(item_id.encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


async def _fetch_forum_body(session: aiohttp.ClientSession, url: str) -> str:
    """Fetch a forum thread page and extract the first message's body text."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return ""
            html_body = await resp.text()
    except Exception:
        log.exception("Failed to fetch forum thread %s", url)
        return ""

    # XenForo wraps each post body in <div class="bbWrapper">…</div>
    m = re.search(
        r'<div class="bbWrapper">(.*?)</div>\s*(?:<div class="message-attachments|</article>)',
        html_body,
        re.DOTALL,
    )
    if not m:
        m = re.search(r'<div class="bbWrapper">(.*?)</div>', html_body, re.DOTALL)
    if not m:
        return ""
    return _strip_html(m.group(1))


async def fetch_steam_news(session: aiohttp.ClientSession) -> list[NewsItem]:
    async with session.get(STEAM_NEWS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    screenshots = await _fetch_steam_screenshots(session)
    items: list[NewsItem] = []
    for raw in data.get("appnews", {}).get("newsitems", []):
        gid = str(raw.get("gid") or raw.get("url") or "")
        if not gid:
            continue
        contents = raw.get("contents", "") or ""
        item_id = f"steam:{gid}"
        body_full = _strip_html(contents)
        image_url = _extract_image_url(contents) or _pick_rotating_image(item_id, screenshots)
        items.append(
            NewsItem(
                source="steam",
                item_id=item_id,
                title=raw.get("title", "Without title"),
                url=raw.get("url", ""),
                published_ts=int(raw.get("date", 0)),
                author=raw.get("author") or raw.get("feedlabel") or "Steam",
                summary=_truncate(body_full),
                body_full=body_full,
                image_url=image_url,
            )
        )
    return items


async def fetch_forum_rss(session: aiohttp.ClientSession) -> list[NewsItem]:
    async with session.get(FORUM_RSS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        body = await resp.text()

    parsed = await asyncio.to_thread(feedparser.parse, body)
    screenshots = await _fetch_steam_screenshots(session)

    raw_entries: list[tuple[NewsItem, str]] = []
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
        summary_text = _strip_html(summary_html)
        item_id = f"forum:{guid}"
        image_url = _extract_image_url(summary_html) or _pick_rotating_image(item_id, screenshots)
        item = NewsItem(
            source="forum",
            item_id=item_id,
            title=entry.get("title", "Without title"),
            url=link,
            published_ts=published_ts,
            author=entry.get("author", "playdeadlock.com"),
            summary=_truncate(summary_text),
            body_full=summary_text,  # filled in below if we can fetch the thread
            image_url=image_url,
        )
        raw_entries.append((item, link))

    # Fetch full bodies in parallel (one HTTP request per thread).
    full_bodies = await asyncio.gather(
        *[_fetch_forum_body(session, link) for _, link in raw_entries],
        return_exceptions=True,
    )
    out: list[NewsItem] = []
    for (item, _), body in zip(raw_entries, full_bodies):
        if isinstance(body, str) and body:
            item.body_full = body
        out.append(item)
    return out


async def fetch_all_news() -> list[NewsItem]:
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
            log.exception("News source failed", exc_info=res)
            continue
        items.extend(res)

    items.sort(key=lambda x: x.published_ts, reverse=True)
    return items


def filter_new(items: Iterable[NewsItem], seen_ids: set[str]) -> list[NewsItem]:
    fresh = [it for it in items if it.item_id not in seen_ids]
    fresh.sort(key=lambda x: x.published_ts)
    return fresh
