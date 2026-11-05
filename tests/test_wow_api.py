"""wow_api.py (Blizzard game data for WoW Forever) and webapi/game_api.py."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import config
import wow_api
from webapi import auth, server

ACCOUNT_PROFILE = {"wow_accounts": [{"id": 1234567, "characters": [{
    "character": {"href": "https://eu.api.blizzard.com/profile/wow/character/davina-asaria?namespace=profile-forever-eu"},
    "protected_character": {"href": "https://eu.api.blizzard.com/profile/user/wow/protected-character/6855-716333?namespace=profile-forever-eu"},
    "name": "Davina", "surname": "Asaria", "id": 716333,
    "playable_class": {"name": "Priest", "id": 5}, "playable_race": {"name": "Troll", "id": 8},
    "gender": {"type": "FEMALE", "name": "Female"}, "faction": {"type": "HORDE", "name": "Horde"}, "level": 12,
}, {"name": "", "id": 1}]}]}


class FakeBlizzard:
    """Answers like Blizzard's API from a {(path, namespace): body} table; 404 otherwise."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    async def request(self, path, namespace, params=None, *, session=None, region=None, token=None, locale="en_US"):
        self.calls.append((path, namespace, region, token))
        if namespace.startswith("bogus"):
            return 403, {}, None
        body = self.answers.get((path, namespace))
        return (200, {"battlenet-namespace": namespace, "x-secret": "no"}, body) if body is not None else (404, {}, None)


class WowApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._config = mock.patch.multiple(config, SERVER_REGION="eu", BLIZZARD_LOCALE="en_GB",
                                           BLIZZARD_STATIC_NAMESPACE="static-forever",
                                           BLIZZARD_DYNAMIC_NAMESPACE="dynamic-forever",
                                           BLIZZARD_PROFILE_NAMESPACE="profile-forever")
        self._config.start()
        wow_api._reference.update(data=None, at=0.0, key=None)

    def tearDown(self):
        self._config.stop()

    def test_namespace(self):
        self.assertEqual(wow_api.namespace("static"), "static-forever-eu")
        self.assertEqual(wow_api.namespace("profile", "us"), "profile-forever-us")

    def test_parse_account_profile(self):
        [c] = wow_api.parse_account_profile(ACCOUNT_PROFILE)  # nameless entries are skipped
        self.assertEqual((c["name"], c["surname"], c["realm_id"], c["blizzard_id"]), ("Davina", "Asaria", 6855, 716333))
        self.assertEqual((c["class_id"], c["class_name"], c["faction"], c["level"]), (5, "Priest", "Horde", 12))
        self.assertEqual(wow_api.parse_account_profile({}), [])

    async def test_account_characters(self):
        bot = SimpleNamespace(blizzard=FakeBlizzard({("/profile/user/wow", "profile-forever-eu"): ACCOUNT_PROFILE}))
        characters = await wow_api.account_characters(bot, "user-token")
        self.assertEqual(characters[0]["name"], "Davina")
        self.assertEqual(bot.blizzard.calls[0][3], "user-token")
        self.assertEqual(await wow_api.account_characters(SimpleNamespace(blizzard=FakeBlizzard()), "t"), [])

    async def test_missing_keys(self):
        with self.assertRaisesRegex(wow_api.WowApiError, "keys"):
            await wow_api.reference(SimpleNamespace(blizzard=None))

    async def test_reference(self):
        fake = FakeBlizzard({
            ("/data/wow/playable-class/index", "static-forever-eu"): {"classes": [{"id": 5, "name": "Priest"}]},
            ("/data/wow/playable-class/5", "static-forever-eu"): {"specializations": [{"id": 256, "name": "Discipline"}]},
            ("/data/wow/playable-specialization/256", "static-forever-eu"): {"role": {"type": "HEALER"}},
            ("/data/wow/profession/index", "static-forever-eu"): {"professions": [
                {"id": 197, "name": "Tailoring"}, {"id": 1, "name": "Test Profession [DNT]"}, {"id": 164, "name": "Blacksmithing"}]},
            ("/data/wow/ruleset/index", "dynamic-forever-eu"): {"rulesets": [{"id": 15}, {"id": 2}]},
            ("/data/wow/ruleset/2", "dynamic-forever-eu"): {"id": 2, "name": "Normal", "status": {"type": "UP"}},
            ("/data/wow/ruleset/15", "dynamic-forever-eu"): {"id": 15, "name": "Roleplay PvP"},
        })
        ref = await wow_api.reference(SimpleNamespace(blizzard=fake))
        self.assertEqual(ref["classes"], [{"id": 5, "name": "Priest", "specs": [{"id": 256, "name": "Discipline", "role": "healer"}]}])
        self.assertEqual([p["name"] for p in ref["professions"]], ["Blacksmithing", "Tailoring"])
        self.assertEqual(ref["rulesets"], [{"id": 2, "name": "Normal"}, {"id": 15, "name": "Roleplay PvP"}])
        self.assertTrue(any("playable-race" in e for e in ref["errors"]))  # missing parts are reported, not fatal
        calls = len(fake.calls)
        await wow_api.reference(SimpleNamespace(blizzard=fake))
        self.assertEqual(len(fake.calls), calls)  # cached

    async def test_lookup_character_tries_paths_in_order(self):
        fake = FakeBlizzard({("/profile/wow/character/davina", "profile-forever-eu"): {"level": 20}})
        bot = SimpleNamespace(blizzard=fake)
        paths = ["/profile/wow/character/{name}-{surname}", "/profile/wow/character/{realm}/{name}",
                 "/profile/wow/character/{name}"]
        with mock.patch.object(config, "CHARACTER_LOOKUP_PATHS", paths), \
                self.assertLogs("wow-apply-bot.wow_api", "WARNING") as logs:
            profile = await wow_api.lookup_character(bot, "Davina", "Asaria")
            self.assertEqual(wow_api.profile_level(profile), 20)
            # Forever has no realms: a {realm} path is skipped with a warning, not filled in or crashed on.
            self.assertEqual([c[0] for c in fake.calls], ["/profile/wow/character/davina-asaria",
                                                          "/profile/wow/character/davina"])
            self.assertIn("unknown placeholders realm", logs.output[0])
            fake.calls.clear()
            await wow_api.lookup_character(bot, "Davina")  # no surname: the {surname} path is skipped
            self.assertEqual([c[0] for c in fake.calls], ["/profile/wow/character/davina"])

    async def test_explore_restrictions(self):
        bot = SimpleNamespace(blizzard=FakeBlizzard({("/data/wow/realm/index", "dynamic-forever-us"): {"realms": []}}))
        for path, namespace, region in [("/oauth/userinfo", "dynamic-forever-eu", None),
                                        ("/data/wow/../oauth", "dynamic-forever-eu", None),
                                        ("/data/wow/realm/index", "x", None),
                                        ("/data/wow/realm/index", "dynamic-forever-eu", "cn")]:
            with self.assertRaises(wow_api.WowApiError, msg=path):
                await wow_api.explore(bot, path, namespace, region)
        result = await wow_api.explore(bot, "data/wow/realm/index", "dynamic-forever-us", "US")
        self.assertEqual((result["status"], result["body"]), (200, {"realms": []}))
        self.assertNotIn("x-secret", result["headers"])  # only the documented Blizzard headers
        self.assertEqual(bot.blizzard.calls[-1][2], "us")
        result = await wow_api.explore(bot, "/data/wow/realm/index", "bogus-eu")
        self.assertEqual(result["status"], 403)
        self.assertIn("doesn't exist", result["meaning"])


class GameApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self.fake = FakeBlizzard({("/data/wow/realm/index", "dynamic-forever-eu"): {"realms": []}})
        bot = SimpleNamespace(guilds=[], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                              store=SimpleNamespace(items=lambda: []), blizzard=self.fake)
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()

    async def explore(self, body):
        resp = await self.client.post("/api/game/explore", json=body, headers={server.CSRF_HEADER: "1"})
        return resp.status, await resp.json()

    async def test_explorer_is_for_admins(self):
        status, _ = await self.explore({"path": "/data/wow/realm/index", "namespace": "dynamic-forever-eu"})
        self.assertEqual(status, 403)
        defaults = await (await self.client.get("/api/game/explorer")).json()
        self.assertFalse(defaults["can_explore"])
        self.assertTrue(defaults["configured"])

    async def test_explore_is_audited(self):
        self.levels = {"moderator", "admin"}
        status, body = await self.explore({"path": "/data/wow/realm/index", "namespace": "dynamic-forever-eu"})
        self.assertEqual((status, body["status"]), (200, 200))
        status, body = await self.explore({"path": "/oauth/check_token", "namespace": "dynamic-forever-eu"})
        self.assertEqual(status, 400)
        import audit
        latest = [e for e in audit.search()[0] if e["action"] == "game.explore"]
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["details"]["status"], 200)


if __name__ == "__main__":
    unittest.main()
