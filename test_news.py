"""Quick offline check that news sources work — no Discord token needed.

Run with:
    python test_news.py
"""
from __future__ import annotations

import asyncio

from news_sources import fetch_all_news
from translator import translate_to_ru


async def main() -> None:
    items = await fetch_all_news()
    print(f"Fetched {len(items)} items total.\n")
    for item in items[:3]:
        title_ru = await translate_to_ru(item.title)
        summary_ru = await translate_to_ru(item.summary[:200])
        print(f"[{item.source:5s}] {item.published_dt:%Y-%m-%d %H:%M}")
        print(f"  Title (en): {item.title}")
        print(f"  Title (ru): {title_ru}")
        print(f"  URL:        {item.url}")
        print(f"  Image:      {item.image_url}")
        print(f"  Summary ru: {summary_ru[:200]}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
