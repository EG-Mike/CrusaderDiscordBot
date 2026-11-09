"""card_stats.py: statistics on the application card (e.g. raid boss kills), and its dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_apply_card import summary as card_summary, texts

import application_card
import card_stats
import config
from cogs import apply
from webapi import auth, server

# Shape of a real WoW Forever answer (/profile/wow/character/{slug}/achievements/statistics, 2026-11-08).
DOC = {"categories": [
    {"name": "Dungeons & Raids", "statistics": [{"id": 932, "name": "Total 5-player dungeons entered", "quantity": 11.0}],
     "sub_categories": [{"name": "Boss Kills", "statistics": [
         {"id": 6140, "name": "Mekgineer Thermaplugg kills (Gnomeregan)", "quantity": 1.0},
         {"id": 1092, "name": "Archmage Arugal kills (Shadowfang Keep)", "quantity": 3.0}]}]},
    {"name": "Deaths", "statistics": [], "sub_categories": [
        {"name": "Dungeons and Raids", "statistics": [{"id": 321, "name": "Total raid and dungeon deaths",
                                                       "quantity": 7.0}]}]},
]}
KARAZHAN = [{"id": 1001, "name": "Attumen the Huntsman kills (Karazhan)", "category": "Dungeons & Raids > Boss Kills",
             "quantity": 4.0},
            {"id": 1002, "name": "Moroes kills (Karazhan)", "category": "Dungeons & Raids > Boss Kills", "quantity": 2.0},
            {"id": 1003, "name": "Prince Malchezaar kills (Karazhan)", "category": "Dungeons & Raids > Boss Kills",
             "quantity": 1.0}]


class ParseTests(unittest.TestCase):
    def test_parse_and_names(self):
        entries = card_stats.parse(DOC)
        self.assertEqual([(e["id"], e["category"]) for e in entries],
                         [(932, "Dungeons & Raids"), (6140, "Dungeons & Raids > Boss Kills"),
                          (1092, "Dungeons & Raids > Boss Kills"), (321, "Deaths > Dungeons and Raids")])
        self.assertEqual(card_stats.values(entries)["1092"], 3.0)
        self.assertEqual(card_stats.split_name("Moroes kills (Karazhan)"), {"label": "Moroes", "group": "Karazhan"})
        self.assertEqual(card_stats.split_name("Rath'mael Kills (Ruins of Lordaeron)"),
                         {"label": "Rath'mael", "group": "Ruins of Lordaeron"})
        self.assertEqual(card_stats.split_name("Total raid and dungeon deaths"),
                         {"label": "Total raid and dungeon deaths", "group": None})
        self.assertEqual(card_stats.parse(None), [])

    def test_card_text_groups_per_instance_and_counts_missing_as_zero(self):
        picks = [{"id": e["id"], "name": e["name"], **card_stats.split_name(e["name"])} for e in KARAZHAN] + [
            {"id": 321, "name": "Total raid and dungeon deaths", **card_stats.split_name("Total raid and dungeon deaths")}]
        stats = {"1001": 4.0, "1002": 2.0, "321": 7.0}  # never killed Malchezaar
        with mock.patch.object(config, "APPLICATION_STATISTICS_TITLE", "Raid boss kills"):
            text = card_stats.card_text(stats, picks)
        self.assertEqual(text, "### Raid boss kills\n"
                               "**Karazhan** Attumen the Huntsman `4` · Moroes `2` · Prince Malchezaar 0\n"
                               "Total raid and dungeon deaths `7`")
        with mock.patch.object(config, "APPLICATION_STATISTICS_TITLE", ""):
            self.assertFalse(card_stats.card_text(stats, picks).startswith("###"))
        self.assertIsNone(card_stats.card_text(stats, []))


class CatalogTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._game = mock.patch.object(config, "GAME_VERSION", "forever")
        self._game.start()

    def tearDown(self):
        self._game.stop()
        super().tearDown()

    def test_seen_and_chosen(self):
        card_stats.record_seen(KARAZHAN)
        card_stats.record_seen(KARAZHAN[:1])  # seen again: no duplicate
        self.assertEqual(len(card_stats.catalog()), 3)
        card_stats.set_chosen([1003, 1001, 1003])
        self.assertEqual([c["id"] for c in card_stats.chosen()], [1003, 1001])  # order kept, once each
        self.assertTrue(next(c for c in card_stats.catalog() if c["id"] == 1001)["chosen"])
        with self.assertRaises(ValueError):
            card_stats.set_chosen([424242])  # never seen
        self.assertEqual([c["id"] for c in card_stats.chosen()], [1003, 1001])  # unchanged

    def test_on_the_card_within_the_limit(self):
        card_stats.record_seen(KARAZHAN)
        card_stats.set_chosen([1001, 1002, 1003])
        cog = apply.ApplyCog.__new__(apply.ApplyCog)
        cog.bot = SimpleNamespace(store=SimpleNamespace(items=lambda: []))
        record = {"applicant_id": 1, "character_name": "Eggmike Thewhite", "character_slug": "eggmike-thewhite",
                  "status": "pending", "class_name": "Priest", "spec": "Holy", "role": "Healer",
                  "gear": card_summary(achievement_stats={"1001": 4.0}), "note": "n" * 1500}
        with mock.patch.object(config, "APPLICATION_STATISTICS_TITLE", "Raid boss kills"):
            text = texts(cog._render_application_view(record, message_id=5))
            lookup = texts(application_card.lookup_view(record["gear"]))
        self.assertIn("### Raid boss kills\n**Karazhan** Attumen the Huntsman `4` · Moroes 0 · Prince Malchezaar 0",
                      text)
        self.assertIn("**Karazhan** Attumen the Huntsman `4`", lookup)
        self.assertLessEqual(len(text), 4000)


class ApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        guild = SimpleNamespace(id=1, name="Test", icon=None, member_count=1, members=[], get_member=lambda i: None)
        self.bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                                   user=None, store=SimpleNamespace(items=lambda: []), get_channel=lambda cid: None,
                                   blizzard=None, get_cog=lambda name: None)
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(config, "GAME_VERSION", "forever")]
        for p in self._patches:
            p.start()
        card_stats.record_seen(KARAZHAN)
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        self.client.session.cookie_jar.update_cookies(
            {auth.SESSION_COOKIE: auth.create_session({"id": "111", "username": "mike_eg"})})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_choose_and_read(self):
        body = await (await self.client.get("/api/applications/statistics")).json()
        self.assertEqual((len(body["catalog"]), body["chosen"], body["can_scan"]), (3, [], False))
        resp = await self.client.put("/api/applications/statistics", json={"stat_ids": [1002, 1001]},
                                     headers=self.headers)
        self.assertEqual([c["label"] for c in (await resp.json())["chosen"]], ["Moroes", "Attumen the Huntsman"])
        resp = await self.client.put("/api/applications/statistics", json={"stat_ids": ["x"]}, headers=self.headers)
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/api/applications/statistics/scan", headers=self.headers)
        self.assertEqual(resp.status, 409)  # no Blizzard keys / roster here
        events = (await (await self.client.get("/api/audit")).json())["events"]
        self.assertIn("applications.statistics", [e["action"] for e in events])

    async def test_status_sends_the_link_templates(self):
        with mock.patch.multiple(config, ITEM_LINK_URL="https://www.wowhead.com/forever/item={id}", SPELL_LINK_URL="",
                                 WOWHEAD_TOOLTIPS_ENABLED=True):
            links = (await (await self.client.get("/api/status")).json())["links"]
        self.assertEqual(links, {"item": "https://www.wowhead.com/forever/item={id}", "spell": None,
                                 "wowhead_tooltips": True})

    async def test_moderators_only(self):
        self.levels = set()
        self.assertEqual((await self.client.get("/api/applications/statistics")).status, 403)
        resp = await self.client.put("/api/applications/statistics", json={"stat_ids": [1001]}, headers=self.headers)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
