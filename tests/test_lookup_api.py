"""webapi/lookup_api.py: the character lookup."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_gear import EQUIPMENT, PROFILE, SPECS

import config
import gear
from webapi import auth, lookup_api, server


class LookupApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        summary = gear.summarise(PROFILE, EQUIPMENT, SPECS)

        async def fake_fetch(_bot, slug, _classes=None):
            if slug != "eggmike-thewhite":
                raise gear.GearError("Blizzard has no profile for this character.")
            return summary

        bot = SimpleNamespace(guilds=[], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), blizzard=object())
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(gear, "fetch", fake_fetch),
                         mock.patch.object(gear, "item_icon_urls", mock.AsyncMock(return_value={})),
                         mock.patch.object(lookup_api.wow_api, "reference", mock.AsyncMock(return_value={"classes": []})),
                         mock.patch.multiple(config, CHARACTER_RULESETS=[1], GAME_VERSION="forever", SERVER_REGION="eu")]
        for p in self._patches:
            p.start()
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_lookup(self):
        resp = await self.client.get("/api/lookup?name=Eggmike Thewhite")
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["character"]["character"], "Eggmike Thewhite")
        self.assertTrue(body["off_ruleset"])  # Normal, while the guild plays PvP
        self.assertIsNone(body["roster"])
        self.assertIn("icon_url", body["character"]["items"][0])

    async def test_not_found_and_access(self):
        resp = await self.client.get("/api/lookup?name=Nobody Here")
        self.assertEqual(resp.status, 404)
        self.assertIn("Couldn't find Nobody Here", (await resp.json())["error"])
        self.assertEqual((await self.client.get("/api/lookup?name=")).status, 400)
        self.levels = {"viewer"}
        self.assertEqual((await self.client.get("/api/lookup?name=Eggmike Thewhite")).status, 403)


if __name__ == "__main__":
    unittest.main()
