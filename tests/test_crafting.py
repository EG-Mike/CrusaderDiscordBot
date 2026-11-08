"""Crafting: crafting.py, the crafting channel (cogs/crafting.py) and the dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import config
import crafting
import roster
import wow_api
from cogs import crafting as crafting_cog
from webapi import auth, server

# Shapes from real WoW Forever answers (2026-11-08).
PROFESSIONS_DOC = {"primaries": [
    {"profession": {"name": "Tailoring", "id": 197},
     "tiers": [{"skill_points": 160, "max_skill_points": 225, "tier": {"name": "Tailoring", "id": 2950},
                "known_recipes": [{"name": "Simple Linen Boots", "id": 6577}, {"name": "Simple Kilt", "id": 6578}]}]},
    {"profession": {"name": "Enchanting", "id": 333},
     "tiers": [{"skill_points": 200, "max_skill_points": 225, "tier": {"name": "Enchanting", "id": 2940},
                "known_recipes": [{"name": "Enchant Bracer - Strength", "id": 7001}]}]}],
    "secondaries": [{"profession": {"name": "Cooking", "id": 185},
                     "tiers": [{"skill_points": 1, "max_skill_points": 75, "known_recipes": [{"id": 1535}]}]}]}
RECIPE_DOC = {"id": 6577, "name": "Simple Linen Boots",
              "crafted_item": {"name": "Simple Linen Boots", "id": 10046}, "crafted_quantity": {"value": 1.0},
              "reagents": [{"reagent": {"name": "Bolt of Linen Cloth", "id": 2996}, "quantity": 2},
                           {"reagent": {"name": "Light Leather", "id": 2318}, "quantity": 1}]}
CATALOG = {"id": 197, "name": "Tailoring",
           "categories": [{"name": "Boots", "recipes": [{"id": 6577, "name": "Simple Linen Boots"}]},
                          {"name": "Legs", "recipes": [{"id": 6578, "name": "Simple Kilt"}]}]}


class Scoped(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._scope = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                          GUILD_ROSTER_GUILD="Encore", ITEM_LINK_URL="https://wh/item={id}")
        self._scope.start()
        roster.store_roster([char(1, 30), char(2, 25, name="Jaina", surname="Proud"),
                             char(3, 40, name="Out", surname="Sider", guild_slug="other")], "encore")
        roster.store_roster([char(3, 40, name="Out", surname="Sider", guild_slug="other")], "other")
        roster.store_professions("eggmike-thewhite", PROFESSIONS_DOC)
        roster.store_professions("out-sider", PROFESSIONS_DOC)  # not in the guild: not a crafter
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=True)

    def tearDown(self):
        self._scope.stop()
        super().tearDown()


class RulesTests(Scoped, unittest.IsolatedAsyncioTestCase):
    def test_known_recipes_of_every_profession(self):
        self.assertEqual(roster.parse_known_recipes(PROFESSIONS_DOC), [1535, 6577, 6578, 7001])
        self.assertEqual(roster.known_recipes(roster.get("eggmike-thewhite")), [1535, 6577, 6578, 7001])

    def test_who_knows_and_overview_are_the_guild_only(self):
        characters = crafting.crafters()
        self.assertEqual({c["slug"] for c in characters}, {"eggmike-thewhite", "jaina-proud"})
        knows = crafting.who_knows(characters)
        self.assertEqual([c["slug"] for c in knows[6577]], ["eggmike-thewhite"])
        overview = crafting.profession_overview(characters)
        self.assertEqual([(r["character"]["slug"], r["skill"]) for r in overview[333]], [("eggmike-thewhite", 200)])
        self.assertEqual([r["skill"] for r in overview[185]], [1])  # secondary professions count too
        self.assertEqual([p["id"] for p in roster.professions(roster.get("eggmike-thewhite"), primary_only=True)],
                         [197, 333])
        roster.store_professions("jaina-proud", {"primaries": [{"profession": {"name": "Tailoring", "id": 197},
                                                                "tiers": [{"skill_points": 220, "max_skill_points": 225,
                                                                           "known_recipes": [{"id": 6577}]}]}]})
        knows = crafting.who_knows(crafting.crafters())
        self.assertEqual([c["slug"] for c in crafting.by_skill(knows[6577], 197)], ["jaina-proud", "eggmike-thewhite"])

    def test_parse_recipe(self):
        self.assertEqual(crafting.parse_recipe(RECIPE_DOC), {
            "recipe_id": 6577, "name": "Simple Linen Boots", "item_id": 10046, "item_name": "Simple Linen Boots",
            "quantity": 1.0, "reagents": [{"id": 2996, "name": "Bolt of Linen Cloth", "quantity": 2},
                                          {"id": 2318, "name": "Light Leather", "quantity": 1}]})
        enchant = crafting.parse_recipe({"id": 7001, "name": "Enchant Bracer - Strength", "reagents": []})
        self.assertIsNone(enchant["item_id"])  # enchants make no item

    async def test_feature_and_remove(self):
        details = mock.AsyncMock(return_value={**crafting.parse_recipe(RECIPE_DOC), "icon_url": "https://i/1.jpg"})
        with mock.patch.object(crafting, "recipe_details", details):
            row = await crafting.add_featured(object(), 6577, {"id": 197, "name": "Tailoring"}, by="Mike (1)")
            self.assertEqual((row["name"], row["profession_name"], row["reagents"][0]["quantity"]),
                             ("Simple Linen Boots", "Tailoring", 2))
            with self.assertRaises(crafting.CraftingError):
                await crafting.add_featured(object(), 6577, {"id": 197, "name": "Tailoring"}, by="Mike (1)")
        self.assertEqual(crafting.remove_featured(6577)["recipe_id"], 6577)
        self.assertEqual(crafting.featured(), [])
        with self.assertRaises(crafting.CraftingError):
            crafting.remove_featured(6577)


class ChannelTests(Scoped):
    def guild(self):
        member = SimpleNamespace(mention="<@111>")
        return SimpleNamespace(get_member=lambda i: member if i == 111 else None)

    def test_profession_lines(self):
        professions = [{"id": 333, "name": "Enchanting"}, {"id": 197, "name": "Tailoring"},
                       {"id": 171, "name": "Alchemy"}]
        overview = crafting.profession_overview(crafting.crafters())
        lines = crafting_cog.profession_lines(professions, overview, self.guild(), {333: "<:ench:1>"})
        self.assertEqual(lines[:3], ["## Professions", "### <:ench:1> Enchanting · 1 crafter",
                                     "`200/225` <@111> (Eggmike Thewhite)"])
        self.assertIn("### Tailoring · 1 crafter", lines)  # no icon: no gap
        self.assertEqual(lines[-1], "-# Nobody in the guild has Alchemy yet.")

    def test_featured_lines(self):
        featured = [{"recipe_id": 6577, "name": "Simple Linen Boots", "profession_id": 197,
                     "profession_name": "Tailoring", "item_id": 10046, "item_name": "Simple Linen Boots",
                     "quantity": 1.0, "reagents": crafting.parse_recipe(RECIPE_DOC)["reagents"]},
                    {"recipe_id": 9999, "name": "Rare Thing", "profession_id": 197, "profession_name": "Tailoring",
                     "item_id": None, "item_name": None, "quantity": 2.0, "reagents": []}]
        lines = crafting_cog.featured_lines(featured, crafting.who_knows(crafting.crafters()), self.guild(),
                                            {197: "<:tail:2>"}, {6577: "<:boots:3>"})
        self.assertEqual(lines, [
            "## ⭐ Featured recipes", "-# Send the materials to one of the crafters and ask them here.",
            "### <:tail:2> Tailoring",
            "<:boots:3> **[Simple Linen Boots](https://wh/item=10046)**",
            "-# Crafters: <@111> (Eggmike Thewhite)",
            "-# Materials: 2× Bolt of Linen Cloth · 1× Light Leather",
            "▫️ **Rare Thing** ×2",
            "-# Nobody in the guild knows this recipe yet."])
        self.assertEqual(crafting_cog.featured_lines([], {}, None, {}, {}), [])


class ApiTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        guild = SimpleNamespace(id=1, name="Test", icon=None, member_count=1, members=[], get_member=lambda i: None)
        self.cog = SimpleNamespace(publish_soon=mock.Mock(), channel_problem=lambda: None,
                                   publish=mock.AsyncMock(return_value="Posted in #crafting."))
        bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), get_channel=lambda cid: None, blizzard=object(),
                              get_cog=lambda name: self.cog if name == "CraftingCog" else None)
        details = {**crafting.parse_recipe(RECIPE_DOC), "icon_url": None}
        self._patches = [
            mock.patch.object(auth, "levels_for", fake_levels),
            mock.patch.object(crafting, "recipe_catalog", mock.AsyncMock(return_value=CATALOG)),
            mock.patch.object(crafting, "recipe_details", mock.AsyncMock(return_value=details)),
            mock.patch.object(crafting, "professions", mock.AsyncMock(return_value=[
                {"id": 197, "name": "Tailoring", "icon_url": None}])),
            mock.patch.object(wow_api, "reference", mock.AsyncMock(return_value={"classes": [{"id": 5,
                                                                                              "name": "Priest"}]})),
            mock.patch("webapi.crafting_api.gear.icon_urls_for", mock.AsyncMock(return_value={})),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        self.client.session.cookie_jar.update_cookies(
            {auth.SESSION_COOKIE: auth.create_session({"id": "111", "username": "mike_eg"})})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_feature_from_the_catalog_and_remove(self):
        resp = await self.client.get("/api/crafting/professions/197")
        recipes = {r["id"]: r for c in (await resp.json())["categories"] for r in c["recipes"]}
        self.assertEqual((recipes[6577]["known_by"], recipes[6577]["featured"]), (1, False))

        resp = await self.client.post("/api/crafting/featured", json={"recipe_id": 1234, "profession_id": 197},
                                      headers=self.headers)
        self.assertEqual(resp.status, 400)  # not in Tailoring's list
        resp = await self.client.post("/api/crafting/featured", json={"recipe_id": 6577, "profession_id": 197},
                                      headers=self.headers)
        self.assertEqual(resp.status, 201)
        self.cog.publish_soon.assert_called_once()

        body = await (await self.client.get("/api/crafting")).json()
        [featured] = body["featured"]
        self.assertEqual(featured["item_url"], "https://wh/item=10046")
        self.assertEqual([c["display_name"] for c in featured["crafters"]], ["Eggmike Thewhite"])
        self.assertEqual(featured["crafters"][0]["class_name"], "Priest")
        self.assertEqual(body["professions"][0]["crafters"][0]["skill"], 160)
        self.assertEqual((featured["crafters"][0]["skill"], featured["crafters"][0]["max"]), (160, 225))

        resp = await self.client.delete("/api/crafting/featured/6577", headers=self.headers)
        self.assertEqual(resp.status, 200)
        resp = await self.client.delete("/api/crafting/featured/6577", headers=self.headers)
        self.assertEqual(resp.status, 404)
        actions = [e["action"] for e in (await (await self.client.get("/api/audit")).json())["events"]]
        self.assertIn("crafting.feature", actions)
        self.assertIn("crafting.unfeature", actions)

    async def test_moderators_only(self):
        self.levels = set()
        self.assertEqual((await self.client.get("/api/crafting")).status, 403)
        resp = await self.client.post("/api/crafting/featured", json={"recipe_id": 6577, "profession_id": 197},
                                      headers=self.headers)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
