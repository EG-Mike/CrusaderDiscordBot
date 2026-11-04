"""emoji_library.py (icon reuse, sources, limits) and its dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import config
import emoji_library as lib
from webapi import auth, server

REAL_FROM_WOWHEAD = lib._from_wowhead

# kind, id -> (name, file id, image url). Flash Heal and Flash of Light share
# Blizzard icon file 135907; Amplify Magic isn't on Blizzard at all.
BLIZZARD = {
    ("spell", 2061): ("Flash Heal", 135907, "https://render.worldofwarcraft.com/us/icons/56/135907.jpg"),
    ("spell", 19750): ("Flash of Light", 135907, "https://render.worldofwarcraft.com/us/icons/56/135907.jpg"),
    ("item", 19019): ("Thunderfury", 135349, "https://render.worldofwarcraft.com/classic-us/icons/56/inv_sword_39.jpg"),
}
WOWHEAD = {
    ("spell", 2061): ("Flash Heal", "spell_holy_flashheal"),
    ("spell", 19750): ("Flash of Light", "spell_holy_flashheal"),
    ("spell", 1008): ("Amplify Magic", "spell_holy_flashheal"),
}


class FakeBlizzard:
    def __init__(self):
        self.calls = []

    async def get_game_data(self, path, namespace, params=None, session=None):
        self.calls.append(path)
        parts = path.split("/")
        if parts[0] == "search":
            name = params["name.en_US"].lower()
            return {"results": [{"data": {"id": gid, "name": {"en_US": n}}}
                                for (kind, gid), (n, _f, _u) in BLIZZARD.items() if kind == parts[1] and name in n.lower()]}
        if parts[0] == "media":
            found = BLIZZARD.get((parts[1], int(parts[2])))
            return {"assets": [{"key": "icon", "value": found[2], "file_data_id": found[1]}]} if found else None
        found = BLIZZARD.get((parts[0], int(parts[1])))
        return {"id": int(parts[1]), "name": found[0]} if found else None


class FakeBot:
    ids = iter(range(1000, 100000))  # unique across bots, like real Discord IDs

    def __init__(self, blizzard=True):
        self.blizzard = FakeBlizzard() if blizzard else None
        self.emoji = {}
        self.created = []

    async def fetch_application_emojis(self):
        return list(self.emoji.values())

    async def create_application_emoji(self, name, image):
        emoji = mock.MagicMock(spec=discord.Emoji)
        emoji.id, emoji.name, emoji.animated = next(FakeBot.ids), name, False
        emoji.delete = mock.AsyncMock(side_effect=lambda: self.emoji.pop(emoji.id, None))
        self.emoji[emoji.id] = emoji
        self.created.append(name)
        return emoji


async def fake_wowhead(kind, game_id):
    found = WOWHEAD.get((kind, game_id))
    if not found:
        return None
    return lib.Resolved(kind, game_id, found[0], lib.WOWHEAD_ICON_URL.format(icon=found[1]), None, found[1], "wowhead")


class LibraryTestCase(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        lib._app_emoji.clear()
        lib._app_emoji_loaded = 0.0
        lib._lock = None
        self._patches = [mock.patch.object(lib, "_from_wowhead", side_effect=fake_wowhead),
                         mock.patch.object(lib, "_download", mock.AsyncMock(return_value=b"jpg"))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()


class HelperTests(unittest.TestCase):
    def test_parse_references(self):
        text = ("https://www.wowhead.com/tbc/spell=2061/flash-heal and wowhead.com/item=19019 "
                "item:22854, spell 1008, again spell=2061")
        self.assertEqual(lib.parse_references(text), [("spell", 2061), ("item", 19019), ("item", 22854), ("spell", 1008)])
        self.assertEqual(lib.parse_references("flash heal"), [])

    def test_icon_from_url(self):
        self.assertEqual(lib.icon_from_url("https://render.worldofwarcraft.com/us/icons/56/135907.jpg"), (135907, None))
        self.assertEqual(lib.icon_from_url("https://wow.zamimg.com/images/wow/icons/large/INV_Sword_39.jpg"),
                         (None, "inv_sword_39"))
        self.assertEqual(lib.icon_from_url(None), (None, None))

    def test_unique_name(self):
        self.assertEqual(lib._unique_name("Flask of Relentless Assault", set()), "flask_of_relentless_assault")
        self.assertEqual(lib._unique_name("inv_sword_39", {"inv_sword_39"}), "inv_sword_39_2")
        long = lib._unique_name("ability_" + "x" * 60, set())
        self.assertLessEqual(len(long), 32)
        self.assertEqual(lib._unique_name("!", set()), "icon")


class AddTests(LibraryTestCase):
    async def test_shared_icons_reuse_one_emoji(self):
        bot = FakeBot()
        first = await lib.add(bot, "spell", 2061, "Mike")
        second = await lib.add(bot, "spell", 19750, "Mike")
        third = await lib.add(bot, "spell", 1008, "Mike")  # only Wowhead knows it, matched by icon name
        self.assertEqual([first["status"], second["status"], third["status"]], ["created", "shared", "shared"])
        self.assertEqual(bot.created, ["spell_holy_flashheal"])
        self.assertEqual([a["name"] for a in third["emoji"]["aliases"]], ["Flash Heal", "Flash of Light", "Amplify Magic"])
        self.assertEqual(third["emoji"]["file_id"], 135907)
        again = await lib.add(bot, "spell", 2061, "Mike")
        self.assertEqual(again["status"], "exists")

    async def test_blizzard_image_is_used_and_classic_icon_names_kept(self):
        bot = FakeBot()
        result = await lib.add(bot, "item", 19019, "Mike")
        self.assertEqual(result["item"]["source"], "blizzard")
        lib._download.assert_awaited_with("https://render.worldofwarcraft.com/classic-us/icons/56/inv_sword_39.jpg")
        self.assertEqual(result["emoji"]["name"], "inv_sword_39")

    async def test_wowhead_only_when_blizzard_has_nothing(self):
        bot = FakeBot()
        await lib.add(bot, "spell", 1008, "Mike")
        lib._download.assert_awaited_with(lib.WOWHEAD_ICON_URL.format(icon="spell_holy_flashheal"))

    async def test_wowhead_can_be_switched_off(self):
        with mock.patch.object(config, "EMOJI_WOWHEAD_FALLBACK", False), \
                mock.patch("aiohttp.ClientSession", side_effect=AssertionError("no request expected")):
            self.assertIsNone(await REAL_FROM_WOWHEAD("spell", 1008))

    async def test_not_found_and_no_keys(self):
        with self.assertRaisesRegex(lib.EmojiError, "No spell with ID 999"):
            await lib.add(FakeBot(), "spell", 999, "Mike")
        with self.assertRaisesRegex(lib.EmojiError, "Blizzard API keys"):
            await lib.add(FakeBot(blizzard=False), "item", 999, "Mike")

    async def test_legacy_per_item_emoji_is_adopted(self):
        bot = FakeBot()
        await bot.create_application_emoji("item_19019", b"")
        bot.created.clear()
        result = await lib.add(bot, "item", 19019, "Mike")
        self.assertEqual((result["status"], result["emoji"]["name"]), ("shared", "item_19019"))
        self.assertEqual(bot.created, [])

    async def test_unknown_emoji_named_after_the_icon_is_adopted(self):
        bot = FakeBot()
        await bot.create_application_emoji("spell_holy_flashheal", b"")
        bot.created.clear()
        result = await lib.add(bot, "spell", 2061, "Mike")
        self.assertEqual((result["status"], result["emoji"]["name"]), ("shared", "spell_holy_flashheal"))
        self.assertEqual(bot.created, [])

    async def test_limit(self):
        bot = FakeBot()
        with mock.patch.object(lib, "MAX_APP_EMOJI", 0):
            with self.assertRaisesRegex(lib.EmojiError, "most Discord allows"):
                await lib.add(bot, "item", 19019, "Mike")

    async def test_emoji_deleted_outside_the_bot_is_forgotten(self):
        bot = FakeBot()
        await lib.add(bot, "item", 19019, "Mike")
        bot.emoji.clear()
        lib._app_emoji_loaded = 0.0
        overview = await lib.overview(bot)
        self.assertEqual(overview["library"], [])
        result = await lib.add(bot, "item", 19019, "Mike")
        self.assertEqual(result["status"], "created")

    async def test_ensure_icon_never_raises(self):
        bot = FakeBot()
        text = await lib.ensure_icon(bot, "spell", 2061)
        self.assertTrue(text.startswith("<:spell_holy_flashheal:"))
        self.assertEqual(await lib.ensure_icon(bot, "spell", 2061), text)  # cached alias
        self.assertEqual(await lib.ensure_icon(bot, "spell", 999), "")
        lib._app_emoji_loaded = 0.0  # a different bot: don't reuse the first one's emoji list
        given = await lib.ensure_icon(FakeBot(blizzard=False), "spell", 4242,
                                      "https://render.worldofwarcraft.com/us/icons/56/555.jpg")
        self.assertTrue(given.startswith("<:spell_4242:"))

    async def test_search(self):
        bot = FakeBot()
        await lib.add(bot, "spell", 2061, "Mike")
        results = await lib.search(bot, "flash")
        states = {(r["kind"], r["name"]): r["state"] for r in results}
        self.assertEqual(states[("spell", "Flash Heal")], "added")
        self.assertEqual(states[("spell", "Flash of Light")], "shared")  # same Blizzard file ID
        by_link = await lib.search(bot, "https://www.wowhead.com/tbc/spell=1008")
        self.assertEqual((by_link[0]["name"], by_link[0]["state"]), ("Amplify Magic", "shared"))
        self.assertEqual(await lib.search(bot, "x"), [])

    async def test_delete(self):
        bot = FakeBot()
        result = await lib.add(bot, "item", 19019, "Mike")
        await lib.delete(bot, result["emoji"]["id"])
        self.assertEqual(bot.emoji, {})
        self.assertIsNone(lib.find_alias("item", 19019))


class EmojiApiTests(LibraryTestCase):
    async def asyncSetUp(self):
        async def fake_levels(_bot, _user_id, fresh=False):
            return {"moderator"}

        self._api_patches = [mock.patch.object(auth, "levels_for", fake_levels),
                             mock.patch.object(config, "FEATURE_EMOJI_ADMIN_ENABLED", True)]
        for p in self._api_patches:
            p.start()
        self.bot = FakeBot()
        self.bot.guilds, self.bot.latency, self.bot.user = [], 0.05, None
        self.bot.is_ready, self.bot.is_closed = (lambda: True), (lambda: False)
        self.bot.store = SimpleNamespace(items=lambda: [])
        self.bot.get_channel = lambda cid: None
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._api_patches:
            p.stop()

    async def test_add_list_delete(self):
        resp = await self.client.post("/api/emoji", headers=self.headers, json={"items": [
            {"kind": "spell", "game_id": 2061}, {"kind": "spell", "game_id": 19750}, {"kind": "spell", "game_id": 999}]})
        results = (await resp.json())["results"]
        self.assertEqual([r["status"] for r in results], ["created", "shared", "error"])
        listing = await (await self.client.get("/api/emoji")).json()
        self.assertEqual((listing["used"], len(listing["library"])), (1, 1))
        self.assertTrue(listing["can_manage"])
        import audit
        self.assertIn("Reused the emoji :spell_holy_flashheal: for Flash of Light", [e["summary"] for e in audit.search()[0]])
        resp = await self.client.delete(f"/api/emoji/{listing['library'][0]['id']}", headers=self.headers)
        self.assertEqual(resp.status, 200)

    async def test_search_and_validation(self):
        found = await (await self.client.get("/api/emoji/search?q=thunderfury")).json()
        self.assertEqual(found[0]["name"], "Thunderfury")
        resp = await self.client.post("/api/emoji", headers=self.headers, json={"items": [{"kind": "mount", "game_id": 1}]})
        self.assertEqual(resp.status, 400)

    async def test_feature_off(self):
        with mock.patch.object(config, "FEATURE_EMOJI_ADMIN_ENABLED", False):
            resp = await self.client.post("/api/emoji", headers=self.headers, json={"items": [{"kind": "item", "game_id": 1}]})
            self.assertEqual(resp.status, 409)


if __name__ == "__main__":
    unittest.main()
