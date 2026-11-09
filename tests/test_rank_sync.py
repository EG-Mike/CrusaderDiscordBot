"""Rank sync: rank_sync.py, the channel post (cogs/rank_sync.py) and the dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import config
import rank_sync
import roster
import wow_api
from cogs import rank_sync as rank_sync_cog
from webapi import auth, server

FRESH, RAIDER = 501, 502


class Scoped(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._scope = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                          GUILD_ROSTER_GUILD="Encore", FRESH_ROLE_ID=FRESH, RANK_SYNC_CHANNEL_ID=0)
        self._scope.start()
        roster.store_roster([
            char(1, 30, guild_rank=7),                                        # Eggmike: Initiate
            char(2, 25, name="Jaina", surname="Proud", guild_rank=5),         # already Fresh
            char(3, 20, name="Alt", surname="Ofjaina", guild_rank=8),
            char(5, 22, name="Rank", surname="Less", guild_rank=None),
        ], "encore")
        roster.store_roster([char(4, 40, name="Out", surname="Sider", guild_slug="other")], "other")
        for slug, member in (("eggmike-thewhite", 11), ("jaina-proud", 12), ("alt-ofjaina", 12), ("out-sider", 13),
                             ("rank-less", 15)):
            roster.link(slug, member, by="Mike", as_moderator=True)
        roster.set_alt("alt-ofjaina", discord_id=12, as_moderator=True)

    def tearDown(self):
        self._scope.stop()
        super().tearDown()


def member(mid, *role_ids, name=None):
    return {"id": mid, "name": name or f"member{mid}", "avatar_url": "", "mention": f"<@{mid}>",
            "role_ids": list(role_ids)}


class RulesTests(Scoped):
    def test_groups(self):
        rank_sync.set_rule(FRESH, 5, by="Mike")
        members = [member(11, FRESH), member(12, FRESH), member(13, FRESH), member(14, FRESH), member(15, FRESH),
                   member(16)]
        entries = {e["id"]: e for e in rank_sync.check(members, roster.characters(), rank_sync.rules())}
        self.assertEqual({k: e["status"] for k, e in entries.items()},
                         {11: "promote", 12: "ok", 13: "not_in_guild", 14: "no_character", 15: "unknown_rank"})
        self.assertEqual(entries[12]["character"]["slug"], "jaina-proud")  # the main, not the lower alt
        self.assertEqual(rank_sync.check(members, roster.characters(), rank_sync.rules())[0]["id"], 11)

    def test_the_highest_rule_wins(self):
        rank_sync.set_rule(FRESH, 5, by="Mike")
        rank_sync.set_rule(RAIDER, 3, by="Mike")
        entries = rank_sync.check([member(12, FRESH, RAIDER)], roster.characters(), rank_sync.rules())
        [entry] = [e for e in entries if e["id"] == 12]
        self.assertEqual((entry["target_rank"], entry["role_id"], entry["status"]), (3, RAIDER, "promote"))

    def test_every_linked_character_shows_up(self):
        rank_sync.set_rule(FRESH, 5, by="Mike")
        entries = {e["id"]: e for e in rank_sync.check([member(11, FRESH), member(12, RAIDER)], roster.characters(),
                                                       rank_sync.rules())}
        self.assertEqual({k: e["status"] for k, e in entries.items()},
                         {11: "promote", 12: "no_rule_role", 13: "not_in_server", 15: "not_in_server"})
        self.assertEqual(entries[13]["character"]["slug"], "out-sider")
        self.assertIsNone(entries[12]["target_rank"])

    def test_rank_names_and_validation(self):
        rank_sync.set_rank_names({0: "Guild Master", 5: " Fresh ", 7: "Initiate", 3: ""})
        self.assertEqual(rank_sync.rank_names(), {0: "Guild Master", 5: "Fresh", 7: "Initiate"})
        self.assertEqual(rank_sync.rank_label(5), "Fresh")
        self.assertEqual(rank_sync.rank_label(6), "Rank 6")
        with self.assertRaises(rank_sync.RankSyncError):
            rank_sync.set_rule(FRESH, 12, by="Mike")
        with self.assertRaises(rank_sync.RankSyncError):
            rank_sync.remove_rule(999)
        seen = {r["rank"]: r for r in rank_sync.ranks_seen(roster.characters())}
        self.assertEqual((seen[7]["count"], seen[7]["examples"]), (1, ["Eggmike Thewhite"]))
        self.assertNotIn(None, seen)

    def test_post_lines(self):
        rank_sync.set_rule(FRESH, 5, by="Mike")
        entries = rank_sync.check([member(11, FRESH)], roster.characters(), rank_sync.rules())
        lines = rank_sync_cog.promote_lines(entries, {5: "Fresh", 7: "Initiate"})
        self.assertEqual(lines, ["### Promote to Fresh · 1", "**Eggmike Thewhite** · <@11> · now Initiate"])
        self.assertEqual(rank_sync_cog.promote_lines([], {}), ["-# Nobody to promote right now."])

    def test_no_guild(self):
        with mock.patch.object(config, "GUILD_ROSTER_GUILD", ""):
            self.assertEqual(rank_sync.rules(), [])
            with self.assertRaises(rank_sync.RankSyncError):
                rank_sync.set_rule(FRESH, 5, by="Mike")


class ApiTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        fresh_role = SimpleNamespace(id=FRESH, name="Fresh")
        discord_member = SimpleNamespace(id=11, display_name="Mike", bot=False, roles=[fresh_role], mention="<@11>",
                                         display_avatar=SimpleNamespace(url="https://a/1.png"))
        guild = SimpleNamespace(id=1, name="Test", icon=None, member_count=1, members=[discord_member],
                                roles=[fresh_role], get_member=lambda i: discord_member if i == 11 else None,
                                get_role=lambda i: fresh_role if i == FRESH else None)
        self.cog = SimpleNamespace(publish_soon=mock.Mock(), channel_problem=lambda: None,
                                   publish=mock.AsyncMock(return_value="Posted in #promotions."))
        bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), get_channel=lambda cid: None, blizzard=object(),
                              get_cog=lambda name: self.cog if name == "RankSyncCog" else None)
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(wow_api, "reference", mock.AsyncMock(return_value={"classes": []}))]
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

    async def test_rules_names_and_the_list(self):
        resp = await self.client.post("/api/rank-sync/rules", json={"role_id": "999", "rank": 5}, headers=self.headers)
        self.assertEqual(resp.status, 400)  # not a role in the server
        resp = await self.client.post("/api/rank-sync/rules", json={"role_id": str(FRESH), "rank": 5},
                                      headers=self.headers)
        self.assertEqual(resp.status, 200)
        self.cog.publish_soon.assert_called_once()
        resp = await self.client.put("/api/rank-sync/ranks", json={"names": {"5": "Fresh", "7": "Initiate"}},
                                     headers=self.headers)
        self.assertEqual(resp.status, 200)

        body = await (await self.client.get("/api/rank-sync")).json()
        self.assertEqual(body["rules"], [{"role_id": str(FRESH), "role_name": "Fresh", "rank": 5,
                                          "rank_label": "Fresh"}])
        self.assertEqual(body["suggested_role_id"], str(FRESH))
        [entry] = [e for e in body["entries"] if e["member"]["id"] == "11"]
        self.assertEqual({e["status"] for e in body["entries"] if e["member"]["id"] != "11"}, {"not_in_server"})
        self.assertEqual((entry["member"]["id"], entry["status"], entry["character"]["rank_label"],
                          entry["target_label"]), ("11", "promote", "Initiate", "Fresh"))
        self.assertEqual({r["rank"]: r["name"] for r in body["ranks"]}[7], "Initiate")

        resp = await self.client.delete(f"/api/rank-sync/rules/{FRESH}", headers=self.headers)
        self.assertEqual(resp.status, 200)
        actions = [e["action"] for e in (await (await self.client.get("/api/audit")).json())["events"]]
        self.assertIn("rank_sync.rule", actions)
        self.assertIn("rank_sync.ranks", actions)

    async def test_moderators_only(self):
        self.levels = set()
        self.assertEqual((await self.client.get("/api/rank-sync")).status, 403)
        resp = await self.client.post("/api/rank-sync/rules", json={"role_id": str(FRESH), "rank": 5},
                                      headers=self.headers)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
