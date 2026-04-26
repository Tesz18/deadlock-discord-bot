"""Quick offline check that news sources work — no Discord token needed.

Run with:
    python test_news.py
"""
from __future__ import annotations

import asyncio

from news_sources import fetch_all_news


async def main() -> None:
    items = await fetch_all_news()
    print(f"Fetched {len(items)} items total.\n")
    for item in items[:5]:
        print(f"[{item.source:5s}] {item.published_dt:%Y-%m-%d %H:%M} — {item.title}")
        print(f"        {item.url}")
        print(f"        {item.summary[:120]}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
