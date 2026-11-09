"""Auction prices: auction_prices.py, the guild chat answers (cogs/auction_prices.py) and the dashboard API."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import discord

import auction_prices
import config
from cogs import auction_prices as auction_cog
from webapi import auth, server

LAST_MODIFIED = "Mon, 9 Nov 2026 12:12:15 GMT"
# Shapes from real WoW Forever answers (2026-11-09, ruleset 1, house 113).
COMMODITIES = {"auctions": [
    {"id": 1, "item": {"id": 2589}, "quantity": 20, "unit_price": 30, "time_left": "LONG"},
    {"id": 2, "item": {"id": 2589}, "quantity": 5, "unit_price": 22, "time_left": "SHORT"},
    {"id": 3, "item": {"id": 2589}, "quantity": 11, "unit_price": 2000023, "time_left": "MEDIUM"},
    {"id": 4, "item": {"id": 14047}, "quantity": 10, "unit_price": 2016, "time_left": "LONG"}]}
ITEMS = {"auctions": [
    {"id": 5, "item": {"id": 3193, "context": 1, "bonus_lists": [12919]}, "buyout": 2100, "quantity": 1,
     "time_left": "SHORT"},
    {"id": 6, "item": {"id": 3193}, "buyout": 5000, "quantity": 2, "time_left": "LONG"},
    {"id": 7, "item": {"id": 4560}, "bid": 100, "quantity": 1, "time_left": "LONG"}]}  # bid only: no price
SEARCH = {"results": [
    {"data": {"id": 2996, "name": {"en_GB": "Bolt of Linen Cloth"}, "quality": {"type": "COMMON"}}},
    {"data": {"id": 2589, "name": {"en_GB": "Linen Cloth"}, "quality": {"type": "COMMON"}}},
    {"data": {"id": 13468, "name": {"en_GB": "Black Lotus"}, "quality": {"type": "UNCOMMON"}}},
    {"data": {"id": 14047, "name": {"en_GB": "Runecloth"}, "quality": {"type": "COMMON"}}}]}


class FakeBlizzard:
    """Answers like Blizzard: the snapshot (304 when unchanged since If-Modified-Since), houses and item search."""

    def __init__(self):
        self.calls = []

    async def request(self, path, namespace, params=None, *, session=None, region=None, token=None, locale="en_US",
                      headers=None):
        self.calls.append((path, (headers or {}).get("If-Modified-Since")))
        if path.endswith("/auctions/index"):
            return 200, {}, {"auctions": [{"id": 113, "name": "Horde Auction House"},
                                          {"id": 114, "name": "Horde Auction House"}]}
        if path == "/data/wow/search/item":
            words = params[f"name.{config.BLIZZARD_LOCALE}"].rstrip("*").lower().split()
            hits = [r for r in SEARCH["results"]
                    if all(w in r["data"]["name"]["en_GB"].lower().split() for w in words)]
            return 200, {}, {"results": hits}
        if "/auctions/" in path:
            if (headers or {}).get("If-Modified-Since") == LAST_MODIFIED:
                return 304, {"Last-Modified": LAST_MODIFIED}, None
            body = COMMODITIES if path.endswith("/commodities") else ITEMS
            if path.split("/")[-1] == "114" or path.split("/")[-2] == "114":
                body = {"auctions": [a for a in body["auctions"] if a["item"]["id"] != 2589]}
            return 200, {"Last-Modified": LAST_MODIFIED}, body
        return 404, {}, None


class Scoped(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._scope = mock.patch.multiple(
            config, GAME_VERSION="forever", SERVER_REGION="eu", BLIZZARD_LOCALE="en_GB", AUCTION_RULESET_ID=1,
            AUCTION_HOUSE_ID=113, AUCTION_PRICE_CHANNEL_ID=777, AUCTION_PRICE_COOLDOWN_SECONDS=30,
            AUCTION_PRICE_TRIGGERS=["price", "price check", "pc", "what is the price of"],
            AUCTION_PRICE_TRIGGER_AFTER_ITEM=True, AUCTION_PRICE_ANSWER_IN="channel", AUCTION_PRICE_DM_FALLBACK=True,
            AUCTION_PRICE_ANSWER_CHANNEL_ID=0,
            ITEM_LINK_URL="https://wh/item={id}")
        self._scope.start()
        auction_prices._search.clear()
        auction_prices._houses.clear()
        self.blizzard = FakeBlizzard()
        self.bot = SimpleNamespace(blizzard=self.blizzard)

    def tearDown(self):
        self._scope.stop()
        super().tearDown()


class WordsTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(auction_prices.money(43322), "4g 33s 22c")
        self.assertEqual(auction_prices.money(43322, compact=True), "4g33s22c")
        self.assertEqual(auction_prices.money(40005), "4g 5c")
        self.assertEqual(auction_prices.money(22), "22c")
        self.assertEqual(auction_prices.money(0), "0c")

    def test_age(self):
        now = 1_762_690_335.0  # 2025-11-09T12:12:15Z
        self.assertEqual(auction_prices.age_text("2025-11-09T12:12:15Z", now + 30), "just now")
        self.assertEqual(auction_prices.age_text("2025-11-09T12:12:15Z", now + 60 * 12), "12mins ago")
        self.assertEqual(auction_prices.age_text("2025-11-09T12:12:15Z", now + 3600 + 60), "1hr ago")
        self.assertEqual(auction_prices.age_text("2025-11-09T12:12:15Z", now + 3 * 3600 + 60), "3hrs ago")
        self.assertEqual(auction_prices.age_text("2025-11-09T12:12:15Z", now + 3 * 86400), "3 days ago")
        self.assertEqual(auction_prices.age_text(None), "unknown age")

    def test_requested_items(self):
        triggers = ["price", "price check", "pc", "what is the price of", "what's the price of"]
        ask = lambda text: auction_prices.requested_items(text, triggers)  # noqa: E731
        self.assertEqual(ask("price [Linen Cloth]"), ["Linen Cloth"])
        self.assertEqual(ask("Price check [Runecloth] [Copper Bar] and [Linen Cloth] [Wool Cloth]"),
                         ["Runecloth", "Copper Bar", "Linen Cloth"])  # at most three
        self.assertEqual(ask("what is the price of [Black Lotus]?"), ["Black Lotus"])
        self.assertEqual(ask("What’s the price of [Black Lotus]"), ["Black Lotus"])  # curly apostrophe
        self.assertEqual(ask("Eggmike: pc [Linen Cloth]"), ["Linen Cloth"])  # a name before it
        self.assertEqual(ask("price of [Linen Cloth]"), ["Linen Cloth"])
        self.assertEqual(ask("[Linen Cloth] is cheap"), [])
        self.assertEqual(ask("spicy [Linen Cloth]"), [])  # "pc" only as a word
        self.assertEqual(ask("price"), [])
        # The bot's own answer, should it come back through the game, isn't a question.
        self.assertEqual(ask("The auction house price for [Linen Cloth] is 22c (1hr ago)"), [])

    def test_phrase_after_the_item(self):
        triggers = ["price", "price check", "pc"]
        after = lambda text: auction_prices.requested_items(text, triggers, after=True)  # noqa: E731
        # Linked items in the guild chat channel come without spaces between them.
        self.assertEqual(after("[Sentinel's Belt][Officer's Belt] price?"), ["Sentinel's Belt", "Officer's Belt"])
        self.assertEqual(after("[Iron Bar] price check"), ["Iron Bar"])
        self.assertEqual(auction_prices.requested_items("[Iron Bar] price?", triggers), [])  # only with `after`
        self.assertEqual(after("[Iron Bar] price is crazy"), [])  # the phrase must end the message
        self.assertEqual(after("The auction house price for [Linen Cloth] is 22c (1hr ago)"), [])

    def test_summarise_uses_the_middle_item_for_sale(self):
        listings = {}
        self.assertEqual(auction_prices.collect(COMMODITIES["auctions"] + ITEMS["auctions"], listings), 6)
        linen = auction_prices.summarise(listings[2589])
        self.assertEqual(linen, {"min_price": 22, "median_price": 30, "quantity": 36, "listings": 3})
        self.assertEqual(auction_prices.summarise(listings[3193])["min_price"], 2100)  # 2 for 5000 is 2500 each
        self.assertNotIn(4560, listings)


class RefreshTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def test_refresh_then_unchanged(self):
        result = await auction_prices.refresh(self.bot)
        self.assertEqual((result["changed"], result["snapshot_at"], result["items"]), (True, "2026-11-09T12:12:15Z", 3))
        self.assertEqual(auction_prices.prices([2589])[2589]["min_price"], 22)
        self.blizzard.calls.clear()
        result = await auction_prices.refresh(self.bot)
        self.assertFalse(result["changed"])
        self.assertEqual([since for _p, since in self.blizzard.calls], [LAST_MODIFIED, LAST_MODIFIED])

    async def test_no_house_picked(self):
        with mock.patch.object(config, "AUCTION_HOUSE_ID", 0):
            with self.assertRaises(auction_prices.AuctionError):
                await auction_prices.refresh(self.bot)

    async def test_lookup_ranks_exact_names_first(self):
        await auction_prices.refresh(self.bot)
        results = await auction_prices.lookup(self.bot, "linen cloth")
        self.assertEqual([r["name"] for r in results], ["Linen Cloth", "Bolt of Linen Cloth"])
        self.assertEqual(results[0]["price"]["quantity"], 36)
        self.assertIsNone(results[1]["price"])
        self.assertEqual((await auction_prices.exact(self.bot, "black lotus"))["id"], 13468)
        self.assertIsNone(await auction_prices.exact(self.bot, "Linen Clth"))

    async def test_a_linked_recipe_counts_as_its_item(self):
        await auction_prices.refresh(self.bot)
        self.assertEqual((await auction_prices.exact(self.bot, "Tailoring: Runecloth"))["id"], 14047)
        self.assertIsNone(await auction_prices.exact(self.bot, "Formula: Nothing Real"))

    async def test_compare_houses(self):
        rows = await auction_prices.compare(self.bot, 1, [113, 114], 2589)
        self.assertEqual([(r["house_id"], r["quantity"]) for r in rows], [(113, 36), (114, 0)])


class ChatTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await auction_prices.refresh(self.bot)
        self.bot.user = SimpleNamespace(id=1)
        self.bot.wait_until_ready = asyncio.Event().wait  # the refresh loop waits forever
        self.cog = auction_cog.AuctionPricesCog(self.bot)
        self.channel = SimpleNamespace(id=777, name="guild-chat", send=mock.AsyncMock())
        self.answers = SimpleNamespace(id=888, name="price-check", send=mock.AsyncMock())
        self.bot.get_channel = lambda cid: {777: self.channel, 888: self.answers}.get(cid)

    async def asyncTearDown(self):
        self.cog.cog_unload()

    def message(self, text, author=2, channel=None, bot=False):
        self.author = SimpleNamespace(id=author, bot=bot, send=mock.AsyncMock())
        return SimpleNamespace(content=text, author=self.author, channel=channel or self.channel, webhook_id=None)

    async def test_answers_in_the_chosen_channel_mentioning_the_asker(self):
        with mock.patch.object(config, "AUCTION_PRICE_ANSWER_CHANNEL_ID", 888):
            message = self.message("price [Runecloth]")
            self.author.mention = "<@2>"
            await self.cog.on_message(message)
        self.channel.send.assert_not_awaited()
        args, kwargs = self.answers.send.await_args
        self.assertTrue(args[0].startswith("<@2> The auction house price for [Runecloth]"))
        self.assertEqual(kwargs["allowed_mentions"].users, [self.author])

    async def test_answers_by_dm(self):
        with mock.patch.object(config, "AUCTION_PRICE_ANSWER_IN", "dm"):
            await self.cog.on_message(self.message("[Runecloth] price?"))
            self.assertIn("20s16c", self.author.send.await_args.args[0])
            self.channel.send.assert_not_awaited()
            # Someone who doesn't take DMs: answered in the channel, unless that's switched off.
            message = self.message("price [Linen Cloth]", author=3)
            self.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                                             "Cannot send messages to this user")
            await self.cog.on_message(message)
            self.assertIn("[Linen Cloth]", self.channel.send.await_args.args[0])
            with mock.patch.object(config, "AUCTION_PRICE_DM_FALLBACK", False):
                message = self.message("price [Runecloth]", author=4)
                self.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
                await self.cog.on_message(message)
            self.assertEqual(self.channel.send.await_count, 1)
            # A bot or the link's notice gets no DM, the channel answer instead.
            await self.cog.on_message(self.message("price [Runecloth]", author=5, bot=True))
            self.author.send.assert_not_awaited()
            self.assertEqual(self.channel.send.await_count, 2)

    async def test_answers_with_the_age(self):
        with mock.patch("auction_prices.time.time", return_value=1_762_690_335.0 + 365 * 86400 + 3 * 3600 + 60):
            await self.cog.on_message(self.message("pc [Linen Cloth] [Black Lotus] [Linen Clth]"))
        text = self.channel.send.await_args.args[0]
        self.assertEqual(text.splitlines(), [
            "The auction house price for [Linen Cloth] is 22c (3hrs ago)",
            "Nobody is selling [Black Lotus] on the auction house (3hrs ago)",
            "I don't know an item called [Linen Clth]"])

    async def test_ignores_other_channels_itself_and_repeats(self):
        await self.cog.on_message(self.message("price [Runecloth]", channel=SimpleNamespace(id=5)))
        await self.cog.on_message(self.message("price [Runecloth]", author=1))  # the bot itself
        self.channel.send.assert_not_awaited()
        await self.cog.on_message(self.message("price [Runecloth]"))
        await self.cog.on_message(self.message("price [Runecloth]"))  # within the cooldown
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIn("20s16c", self.channel.send.await_args.args[0])


class ApiTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        guild = SimpleNamespace(id=1, name="Test", icon=None, member_count=1, members=[], get_member=lambda i: None)
        self.cog = SimpleNamespace(problem=lambda: None, last_problem=None, refresh_soon=mock.Mock(),
                                   refresh=mock.AsyncMock(return_value={"changed": True, "items": 3,
                                                                        "snapshot_at": "2026-11-09T12:12:15Z",
                                                                        "auctions": 6}))
        bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), get_channel=lambda cid: None,
                              blizzard=self.blizzard, get_cog=lambda name: self.cog if name == "AuctionPricesCog" else None)
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch("webapi.auction_api.wow_api.reference",
                                    mock.AsyncMock(return_value={"rulesets": [{"id": 1, "name": "PvP"}]}))]
        for p in self._patches:
            p.start()
        await auction_prices.refresh(self.bot)
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        self.client.session.cookie_jar.update_cookies(
            {auth.SESSION_COOKIE: auth.create_session({"id": "111", "username": "mike_eg"})})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_overview_and_search(self):
        body = await (await self.client.get("/api/auction")).json()
        self.assertEqual((body["house_id"], body["house_name"], body["snapshot"]["items"]),
                         (113, "Horde Auction House", 3))
        body = await (await self.client.get("/api/auction/search?q=linen")).json()
        first = body["items"][0]
        self.assertEqual((first["name"], first["price"]["min_price"], first["url"]),
                         ("Linen Cloth", 22, "https://wh/item=2589"))

    async def test_pick_a_house_saves_the_settings(self):
        resp = await self.client.post("/api/auction/house", json={"ruleset_id": 1, "house_id": 999},
                                      headers=self.headers)
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/api/auction/house", json={"ruleset_id": 1, "house_id": 114},
                                      headers=self.headers)
        self.assertEqual(resp.status, 200)
        self.assertEqual(config.AUCTION_HOUSE_ID, 114)  # applies straight away
        self.cog.refresh_soon.assert_called_once()
        resp = await self.client.post("/api/auction/compare", json={"ruleset_id": 1, "house_ids": [113, 114],
                                                                   "item": "Linen Cloth"}, headers=self.headers)
        self.assertEqual([h["quantity"] for h in (await resp.json())["houses"]], [36, 0])
        actions = [e["action"] for e in (await (await self.client.get("/api/audit")).json())["events"]]
        self.assertIn("auction.house", actions)

    async def test_try_a_message(self):
        self.cog.answer_lines = mock.AsyncMock(return_value=["The auction house price for [Linen Cloth] is 22c (1hr ago)"])
        body = await (await self.client.post("/api/auction/try", json={"message": "[Linen Cloth] pc"},
                                             headers=self.headers)).json()
        self.assertEqual((body["items"], body["answer"]),
                         (["Linen Cloth"], "The auction house price for [Linen Cloth] is 22c (1hr ago)"))
        body = await (await self.client.post("/api/auction/try", json={"message": "anyone near ah?"},
                                             headers=self.headers)).json()
        self.assertEqual((body["items"], body["answer"]), ([], None))
        resp = await self.client.post("/api/auction/try", json={"message": ""}, headers=self.headers)
        self.assertEqual(resp.status, 400)

    async def test_moderators_only(self):
        self.levels = set()
        self.assertEqual((await self.client.get("/api/auction")).status, 403)
        self.assertEqual((await self.client.get("/api/auction/search?q=linen")).status, 403)
        resp = await self.client.post("/api/auction/house", json={"ruleset_id": 1, "house_id": 114},
                                      headers=self.headers)
        self.assertEqual(resp.status, 403)
        resp = await self.client.post("/api/auction/try", json={"message": "price [Linen Cloth]"}, headers=self.headers)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
