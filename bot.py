"""Deadlock news Discord bot.

Posts new patch notes / announcements about the game Deadlock to a configured
text channel on each Discord server.

Slash commands:
- /setchannel #channel  — set where news go (admin only)
- /news                 — manually post the latest 3 news items
- /status               — show current config and last poll time
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from news_sources import NewsItem, fetch_all_news, filter_new
from storage import load_config, load_seen, save_config, save_seen
from translator import chunk_for_discord, translate_to_ru

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("deadlock-bot")

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
POLL_INTERVAL_MINUTES = max(1, int(os.environ.get("POLL_INTERVAL_MINUTES", "5")))
FULL_TRANSLATION_THREAD = os.environ.get(
    "POST_FULL_TRANSLATION_THREAD", "true"
).strip().lower() in ("1", "true", "yes", "on")
# Hard cap on how many 1900-char messages a single thread may produce.
MAX_THREAD_CHUNKS = 12
# Cap body length before translation to keep latency and Google Translate calls reasonable.
MAX_BODY_CHARS = 18000

SOURCE_LABELS = {
    "steam": "Steam News",
    "forum": "playdeadlock.com",
}
SOURCE_LABELS_RU = {
    "steam": "Steam — новости",
    "forum": "Форум playdeadlock.com",
}
SOURCE_COLORS = {
    "steam": discord.Color.from_rgb(23, 26, 33),
    "forum": discord.Color.orange(),
}


class DeadlockBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # We don't need privileged intents — only public channel posting.
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # guild_id (str) -> channel_id (int)
        self.channels: dict[str, int] = load_config()
        self.seen_ids: list[str] = load_seen()
        self.last_poll: datetime | None = None
        self._synced_guilds: set[int] = set()

    async def setup_hook(self) -> None:
        # Per-guild sync happens in on_ready (see below) for instant availability.
        # Global sync as a fallback so commands also appear in any guild added later
        # — global sync can take up to 1h to propagate but doesn't hurt.
        try:
            await self.tree.sync()
        except Exception:
            log.exception("Global command sync failed")
        self.poll_news.change_interval(minutes=POLL_INTERVAL_MINUTES)
        self.poll_news.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")
        log.info("Configured channels: %s", self.channels)
        await self._sync_all_guilds()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined new guild: %s (%s)", guild.name, guild.id)
        await self._sync_guild(guild)

    async def _sync_all_guilds(self) -> None:
        for guild in self.guilds:
            await self._sync_guild(guild)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        if guild.id in self._synced_guilds:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self._synced_guilds.add(guild.id)
            log.info("Synced %d slash commands to guild %s (%s)", len(synced), guild.name, guild.id)
        except Exception:
            log.exception("Failed to sync slash commands to guild %s", guild.id)

    @tasks.loop(minutes=5)
    async def poll_news(self) -> None:
        try:
            log.info("Polling news sources…")
            items = await fetch_all_news()
            self.last_poll = datetime.now(tz=timezone.utc)
            seen_set = set(self.seen_ids)
            fresh = filter_new(items, seen_set)
            if not fresh:
                log.info("No new items.")
                return
            log.info("Found %d new items.", len(fresh))

            # On the very first run with an empty seen.json, don't spam the channel
            # with the entire history. Mark all as seen and only forward going forward.
            if not self.seen_ids:
                log.info("First run detected — marking %d existing items as seen.", len(items))
                self.seen_ids = [it.item_id for it in items]
                save_seen(self.seen_ids)
                return

            for item in fresh:
                await self._broadcast(item)
                self.seen_ids.append(item.item_id)
            save_seen(self.seen_ids)
        except Exception:
            log.exception("poll_news failed")

    @poll_news.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def _broadcast(self, item: NewsItem) -> None:
        """Send the item to every configured channel."""
        if not self.channels:
            log.info("No channels configured — skipping post for %s", item.item_id)
            return
        embed = await build_embed(item)
        for guild_id, channel_id in list(self.channels.items()):
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.NotFound:
                    log.warning(
                        "Channel %s not found for guild %s — removing from config.",
                        channel_id,
                        guild_id,
                    )
                    self.channels.pop(guild_id, None)
                    save_config(self.channels)
                    continue
                except discord.Forbidden:
                    log.warning("No access to channel %s in guild %s.", channel_id, guild_id)
                    continue
            try:
                message = await channel.send(embed=embed)
            except discord.DiscordException:
                log.exception("Failed to post to channel %s", channel_id)
                continue
            await self._post_full_translation_thread(message, item)


    async def _post_full_translation_thread(
        self, message: discord.Message, item: NewsItem
    ) -> None:
        if not FULL_TRANSLATION_THREAD:
            return
        body = item.body_full or item.summary
        if not body:
            return
        # Don't bother making a thread when the embed already shows the entire body.
        if len(body) <= 380:
            return
        truncated_note = ""
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS]
            truncated_note = f"\n\n…Сокращено. Полный текст на источнике: {item.url}"
        try:
            thread = await message.create_thread(
                name=f"📜 Полный перевод"[:100],
                auto_archive_duration=10080,  # 7 days
            )
        except discord.Forbidden:
            log.warning(
                "No permission to create threads in channel %s; skipping full translation",
                message.channel.id,
            )
            return
        except discord.HTTPException:
            log.exception("create_thread failed")
            return

        try:
            translated = await translate_to_ru(body)
        except Exception:
            log.exception("Translation failed for %s", item.item_id)
            translated = body

        if truncated_note:
            translated = (translated or "") + truncated_note
        chunks = chunk_for_discord(translated)
        if len(chunks) > MAX_THREAD_CHUNKS:
            chunks = chunks[:MAX_THREAD_CHUNKS]
            chunks[-1] += f"\n\n…Сокращено. Полный текст на источнике: {item.url}"

        for chunk in chunks:
            try:
                await thread.send(chunk)
            except discord.DiscordException:
                log.exception("Failed to post chunk to thread %s", thread.id)
                break


async def build_embed(item: NewsItem) -> discord.Embed:
    color = SOURCE_COLORS.get(item.source, discord.Color.blurple())
    title_ru, summary_ru = await asyncio.gather(
        translate_to_ru(item.title),
        translate_to_ru(item.summary),
    )
    embed = discord.Embed(
        title=(title_ru or item.title)[:256],
        url=item.url,
        description=summary_ru or item.summary or "(no preview)",
        color=color,
        timestamp=item.published_dt,
    )
    embed.set_author(name=f"Deadlock — {SOURCE_LABELS_RU.get(item.source, item.source)}")
    if item.image_url:
        embed.set_image(url=item.image_url)
    if item.author:
        embed.set_footer(text=item.author)
    return embed


bot = DeadlockBot()


@bot.tree.command(name="setchannel", description="Set the channel for Deadlock news (admin only).")
@app_commands.describe(channel="Text channel where news will be posted")
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command works on a server only.", ephemeral=True)
        return
    perms = channel.permissions_for(interaction.guild.me) if interaction.guild.me else None
    if perms is None or not (perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"I don't have permission to send messages / embed links in {channel.mention}. "
            "Please grant those permissions and try again.",
            ephemeral=True,
        )
        return

    bot.channels[str(interaction.guild.id)] = channel.id
    save_config(bot.channels)
    await interaction.response.send_message(
        f"Deadlock news will now be posted in {channel.mention}.", ephemeral=True
    )


@bot.tree.command(name="news", description="Post the 3 latest Deadlock news items right now.")
async def news(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)
    items = await fetch_all_news()
    if not items:
        await interaction.followup.send("Couldn't fetch news right now, try again later.", ephemeral=True)
        return
    target = interaction.channel
    for item in items[:3]:
        try:
            embed = await build_embed(item)
            message = await target.send(embed=embed)
        except discord.DiscordException:
            log.exception("Failed to post manual news")
            continue
        await bot._post_full_translation_thread(message, item)
    await interaction.followup.send("Done.", ephemeral=True)


@bot.tree.command(name="status", description="Show current bot configuration and last poll time.")
async def status(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command works on a server only.", ephemeral=True)
        return
    channel_id = bot.channels.get(str(interaction.guild.id))
    if channel_id:
        channel_repr = f"<#{channel_id}>"
    else:
        channel_repr = "not configured (use /setchannel)"
    last = bot.last_poll.strftime("%Y-%m-%d %H:%M UTC") if bot.last_poll else "never"
    msg = (
        f"**Channel:** {channel_repr}\n"
        f"**Poll interval:** every {POLL_INTERVAL_MINUTES} min\n"
        f"**Last poll:** {last}\n"
        f"**Tracked items:** {len(bot.seen_ids)}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and put your bot token there."
        )
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
