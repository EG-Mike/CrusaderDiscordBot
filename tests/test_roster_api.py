"""webapi/roster_api.py: the Roster and Leveling pages' API."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import config
import roster
from cogs import roster as roster_cog
from webapi import auth, server

REF = {"classes": [{"id": 5, "name": "Priest", "specs": [{"id": 256, "name": "Discipline", "role": None},
                                                        {"id": 258, "name": "Shadow Magic", "role": None}]}],
       "rulesets": [{"id": 1, "name": "PvP"}]}


class RosterApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        member = SimpleNamespace(id=111, display_name="Mike", name="mike_eg", bot=False,
                                 display_avatar=SimpleNamespace(url="https://cdn/a.png"))
        guild = SimpleNamespace(id=1, name="Test", icon=None, member_count=1, members=[member],
                                get_member=lambda i: member if i == 111 else None)
        cogs = {"RosterCog": object()}
        bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), get_cog=lambda n: cogs.get(n),
                              get_channel=lambda cid: None, blizzard=None)
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                             GUILD_ROSTER_GUILD="Encore", LEVELING_MILESTONES=[10, 20]),
                         mock.patch.object(roster_cog, "reference", mock.AsyncMock(return_value=REF)),
                         mock.patch("webapi.roster_api.reference", mock.AsyncMock(return_value=REF))]
        for p in self._patches:
            p.start()
        roster.store_roster([char(1, 15)], "encore")
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def act(self, body):
        resp = await self.client.post("/api/roster/characters/eggmike-thewhite", json=body, headers=self.headers)
        return resp.status, await resp.json()

    async def test_overview_and_actions(self):
        data = await (await self.client.get("/api/roster")).json()
        self.assertEqual(data["guild_name"], "Encore")
        self.assertEqual(data["characters"][0]["display_name"], "Eggmike Thewhite")
        status, c = await self.act({"action": "link", "discord_id": "111"})
        self.assertEqual((status, c["discord_id"], c["is_main"], c["member"]["name"]), (200, "111", True, "Mike"))
        status, body = await self.act({"action": "link", "discord_id": "999"})
        self.assertEqual(status, 400)
        status, c = await self.act({"action": "specs", "main_spec_id": 256, "off_spec_id": "258"})
        self.assertEqual((c["main_spec_id"], c["off_spec_id"]), (256, 258))
        status, body = await self.act({"action": "specs", "main_spec_id": 71})
        self.assertIn("own class", body["error"])
        status, c = await self.act({"action": "unlink"})
        self.assertIsNone(c["discord_id"])
        members = await (await self.client.get("/api/roster/members?q=mik")).json()
        self.assertEqual(members["members"][0]["id"], "111")
        import audit
        self.assertIn("Linked Eggmike Thewhite", [e["summary"] for e in audit.search()[0]])

    async def test_leveling(self):
        roster.store_roster([char(1, 20)], "encore")
        data = await (await self.client.get("/api/leveling")).json()
        self.assertEqual(data["leaderboard"][0]["level"], 20)
        self.assertEqual((data["events"][0]["old_level"], data["events"][0]["milestone"]), (15, 20))

    async def test_moderators_only(self):
        self.levels = {"viewer"}
        resp = await self.client.get("/api/roster")
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
