"""
Auction prices, Discord side: keeps the guild's auction house prices fresh
(auction_prices.refresh, every AUCTION_REFRESH_MINUTES) and answers price
questions in the guild chat channel (AUCTION_PRICE_CHANNEL_ID, the channel
linked to the in-game guild chat).

Someone writes e.g. "price [Linen Cloth]", "pc [Linen Cloth] [Wool Cloth]"
or "what is the price of [Runecloth]?" (the phrases are a setting); the bot
answers each item on its own line with the "Price answer" bot messages.
Linked items in the game arrive in square brackets. Messages from the bot
itself are never read, and the same item is answered at most once per
AUCTION_PRICE_COOLDOWN_SECONDS per channel. Every setting here is read when
it's used, so changes apply without a restart.
"""

import asyncio
import logging
import time
from typing import Dict, List, Tuple

import discord
from discord.ext import commands, tasks

import auction_prices
import bot_messages
import config

log = logging.getLogger("wow-apply-bot.auction_prices")


def answer_line(name: str, found, snapshot_at) -> str:
    """One line of the answer for an item asked about (found: auction_prices.exact's result or None)."""
    age = auction_prices.age_text(snapshot_at)
    if found is None:
        return bot_messages.text("auction_price_unknown_item", item=name)
    price = found.get("price")
    if not price:
        return bot_messages.text("auction_price_not_listed", item=found["name"], age=age)
    return bot_messages.text("auction_price", item=found["name"],
                             price=auction_prices.money(price["min_price"], compact=True),
                             median=auction_prices.money(price["median_price"], compact=True),
                             quantity=price["quantity"], age=age)


class AuctionPricesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_refresh = 0.0
        self._refresh_lock = asyncio.Lock()
        self._answered: Dict[Tuple[int, str], float] = {}
        self.last_problem = None
        self.refresh_loop.start()

    def cog_unload(self):
        self.refresh_loop.cancel()

    # --- prices ------------------------------------------------------------------------------------

    def problem(self):
        """Why prices can't be read, or None."""
        if getattr(self.bot, "blizzard", None) is None:
            return "Blizzard API keys aren't set up (Settings, General, Integrations)."
        if auction_prices.house_key() is None:
            return "No auction house picked yet."
        return None

    async def refresh(self, force: bool = False) -> dict:
        """Reads a newer snapshot when there is one; raises auction_prices.AuctionError."""
        async with self._refresh_lock:
            self._last_refresh = time.monotonic()
            try:
                result = await auction_prices.refresh(self.bot, force=force)
            except auction_prices.AuctionError as exc:
                self.last_problem = str(exc)
                raise
            self.last_problem = None
            return result

    def refresh_soon(self):
        """After the house changed on the dashboard."""
        async def run():
            try:
                await self.refresh(force=True)
            except auction_prices.AuctionError as exc:
                log.warning("Reading the auction house failed: %s", exc)
            except Exception:
                log.exception("Reading the auction house failed")

        asyncio.ensure_future(run())

    @tasks.loop(minutes=1)
    async def refresh_loop(self):
        every = max(5, int(config.AUCTION_REFRESH_MINUTES or 15)) * 60
        if self.problem() or (self._last_refresh and time.monotonic() - self._last_refresh < every):
            return
        try:
            await self.refresh()
        except auction_prices.AuctionError as exc:
            log.warning("Reading the auction house failed: %s", exc)
        except Exception:
            log.exception("Reading the auction house failed")

    @refresh_loop.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # --- price questions in the guild chat -----------------------------------------------------------

    def _fresh_items(self, channel_id: int, names: List[str]) -> List[str]:
        """The names not answered in this channel within the cooldown (and marks them answered)."""
        now, cooldown = time.monotonic(), max(0, int(config.AUCTION_PRICE_COOLDOWN_SECONDS or 0))
        self._answered = {k: t for k, t in self._answered.items() if now - t < max(cooldown, 1)}
        fresh = []
        for name in names:
            key = (channel_id, name.lower())
            if cooldown and key in self._answered:
                continue
            self._answered[key] = now
            fresh.append(name)
        return fresh

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        channel_id = int(config.AUCTION_PRICE_CHANNEL_ID or 0)
        if not channel_id or message.channel.id != channel_id:
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        names = auction_prices.requested_items(message.content, config.AUCTION_PRICE_TRIGGERS or [])
        if not names:
            return
        if self.problem():
            log.info("Price question in the guild chat not answered: %s", self.problem())
            return
        names = self._fresh_items(channel_id, names)
        if not names:
            return
        snapshot = await asyncio.to_thread(auction_prices.snapshot)
        if snapshot is None:
            log.info("Price question in the guild chat not answered: no auction house data yet")
            return
        lines = []
        for name in names:
            try:
                found = await auction_prices.exact(self.bot, name)
            except auction_prices.AuctionError as exc:
                log.warning("Price question for %r not answered: %s", name, exc)
                continue
            lines.append(answer_line(name, found, snapshot["snapshot_at"]))
        if not lines:
            return
        try:
            await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Couldn't answer a price question in #%s: %s", getattr(message.channel, "name", "?"), exc)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuctionPricesCog(bot))
