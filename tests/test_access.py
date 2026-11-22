"""webapi/access.py and webapi/auth.py: who may use which dashboard route,
and the checks for a dashboard reachable from the internet."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import config
from webapi import access, auth, server


def _bot():
    user = SimpleNamespace(name="Crusader", display_avatar=SimpleNamespace(url="https://cdn/avatar.png"))
    guild = SimpleNamespace(id=1, name="Test server", icon=None, member_count=3, get_member=lambda _id: None,
                            get_role=lambda _id: None, roles=[], channels=[], owner_id=999,
                            me=SimpleNamespace(nick=None, guild_permissions=SimpleNamespace(change_nickname=True)))
    return SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                           user=user, store=SimpleNamespace(items=lambda: []), get_guild=lambda _id: guild,
                           get_cog=lambda _name: None, application_id=123)


def _member(user_id=5, *, role_ids=(), manage_roles=False):
    m = mock.Mock(spec=discord.Member)
    m.id = user_id
    m.roles = [SimpleNamespace(id=r) for r in role_ids]
    m.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
    return m


class RouteTableTests(unittest.TestCase):
    def test_every_api_route_has_a_level_and_no_entry_is_stale(self):
        app = server.create_app(_bot())
        registered = set()
        for route in app.router.routes():
            path = route.resource.canonical if route.resource is not None else ""
            if not path.startswith("/api") or route.method == "HEAD":
                continue
            registered.add((route.method, path))
            self.assertIn(access.required_level(route.method, path), (access.ANY,) + access.LEVELS,
                          f"{route.method} {path} has no access level in webapi/access.py ROUTES")
        self.assertEqual(set(access.ROUTES) - registered, set(), "ROUTES lists routes that don't exist")

    def test_sensitive_routes_are_admin_only(self):
        for key in [("GET", "/api/settings"), ("PUT", "/api/settings/{key}"), ("PUT", "/api/integrations/{key}"),
                    ("GET", "/api/integrations"), ("POST", "/api/bot/restart"), ("POST", "/api/setup/complete")]:
            self.assertEqual(access.ROUTES[key], access.ADMIN, key)

    def test_levels_include_the_lower_ones(self):
        self.assertTrue(access.allows({"admin"}, access.REVIEWER))
        self.assertTrue(access.allows({"moderator"}, access.VIEWER))
        self.assertFalse(access.allows({"moderator"}, access.ADMIN))
        self.assertFalse(access.allows({"reviewer"}, access.VIEWER))  # reviewers only see applications
        self.assertFalse(access.allows({"viewer"}, access.REVIEWER))
        self.assertTrue(access.allows({"viewer"}, access.ANY))
        self.assertFalse(access.allows(set(), access.ANY))
        self.assertFalse(access.allows({"something-else"}, access.ANY))


class MemberLevelTests(unittest.TestCase):
    def setUp(self):
        self._patches = [mock.patch.object(config, name, value) for name, value in {
            "MOD_ROLE_ID": 10, "APPROVER_ROLE_ID": 20,
            "WEB_ACCESS_ADMIN_ROLE_IDS": {"30": "Officer"},  # as the dashboard saves it
            "WEB_ACCESS_VIEWER_ROLE_IDS": {40: "Raider"}, "WEB_ACCESS_VIEWER_ENABLED": False,
        }.items()]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_roles_give_levels(self):
        self.assertEqual(auth.levels_from_member(_member(role_ids=[30])), {"admin"})
        self.assertEqual(auth.levels_from_member(_member(role_ids=[10])), {"moderator"})
        self.assertEqual(auth.levels_from_member(_member(manage_roles=True)), {"moderator"})
        self.assertEqual(auth.levels_from_member(_member(role_ids=[20])), {"reviewer"})
        self.assertEqual(auth.levels_from_member(_member(user_id=999), guild_owner_id=999), {"admin"})
        self.assertEqual(auth.levels_from_member(_member(role_ids=[1, 2])), set())

    def test_viewer_roles_count_only_while_switched_on(self):
        self.assertEqual(auth.levels_from_member(_member(role_ids=[40])), set())
        with mock.patch.object(config, "WEB_ACCESS_VIEWER_ENABLED", True):
            self.assertEqual(auth.levels_from_member(_member(role_ids=[40])), {"viewer"})

    def test_levels_for_adds_env_admins_and_implied_levels(self):
        guild = SimpleNamespace(owner_id=999, get_member=lambda _id: _member(role_ids=[20]))
        bot = SimpleNamespace(guilds=[guild])
        auth.forget_levels()
        with mock.patch.dict("os.environ", {"WEB_ADMIN_USER_IDS": "7"}):
            levels = self._run(auth.levels_for(bot, 7, fresh=True))
        self.assertEqual(levels, {"admin", "moderator", "reviewer", "viewer"})
        self.assertEqual(self._run(auth.levels_for(bot, 8, fresh=True)), {"reviewer"})
        auth.forget_levels()

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.run(coro)


class RequestCheckTests(unittest.TestCase):
    def request(self, remote, forwarded=None, origin=None, host="dash.example"):
        headers = {}
        if forwarded is not None:
            headers["X-Forwarded-For"] = forwarded
        if origin is not None:
            headers["Origin"] = origin
        return SimpleNamespace(remote=remote, headers=headers, host=host)

    def test_client_ip_trusts_only_the_proxy(self):
        self.assertEqual(access.client_ip(self.request("203.0.113.9")), "203.0.113.9")
        # Someone on the internet can't pick their own address.
        self.assertEqual(access.client_ip(self.request("203.0.113.9", "1.2.3.4")), "203.0.113.9")
        # Caddy on the Docker network: its last hop is the visitor, earlier ones could be made up.
        self.assertEqual(access.client_ip(self.request("172.18.0.5", "1.2.3.4, 198.51.100.7")), "198.51.100.7")
        self.assertEqual(access.client_ip(self.request("127.0.0.1")), "127.0.0.1")

    def test_rate_limiter(self):
        limiter = access.RateLimiter(limit=2, seconds=10)
        self.assertTrue(limiter.hit("a", now=0))
        self.assertTrue(limiter.hit("a", now=1))
        self.assertFalse(limiter.hit("a", now=2))
        self.assertTrue(limiter.hit("b", now=2))
        self.assertTrue(limiter.hit("a", now=11.5))  # the first hits expired

    def test_origin(self):
        base = "https://dash.example"
        self.assertTrue(access.origin_allowed(self.request("1.1.1.1"), base))
        self.assertTrue(access.origin_allowed(self.request("1.1.1.1", origin="https://dash.example"), base))
        self.assertTrue(access.origin_allowed(self.request("1.1.1.1", origin="http://localhost:5173",
                                                          host="localhost:5173"), base))
        self.assertFalse(access.origin_allowed(self.request("1.1.1.1", origin="https://evil.example"), base))
        self.assertFalse(access.origin_allowed(self.request("1.1.1.1", origin="null"), base))


class AccessApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(access, "LOGIN_LIMIT", access.RateLimiter(limit=3, seconds=600))]
        for p in self._patches:
            p.start()
        self.client = TestClient(TestServer(server.create_app(_bot())))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_settings_and_keys_are_for_admins(self):
        self.assertEqual((await self.client.get("/api/settings")).status, 403)
        self.assertEqual((await self.client.get("/api/integrations")).status, 403)
        resp = await self.client.put("/api/settings/MOD_ROLE_ID", json={"value": 1}, headers=self.headers)
        self.assertEqual(resp.status, 403)
        self.assertIn("admins", (await resp.json())["error"])
        self.levels = {"admin"}
        self.assertEqual((await self.client.get("/api/settings")).status, 200)

    async def test_reviewers_and_viewers_get_a_trimmed_status(self):
        self.levels = {"viewer"}
        body = await (await self.client.get("/api/status")).json()
        self.assertEqual((body["warnings"], body["missing_setup"], body["restart_available"]), ([], [], False))
        self.assertEqual(body["counts"]["pending_applications"], 0)
        self.assertEqual((await self.client.get("/api/audit")).status, 403)
        self.levels = {"admin"}
        body = await (await self.client.get("/api/status")).json()
        self.assertTrue(body["missing_setup"])  # nothing is set up in the test

    async def test_requests_from_another_site_are_refused(self):
        resp = await self.client.post("/api/roster/sync", json={},
                                      headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)
        self.assertIn("another website", (await resp.json())["error"])

    async def test_security_headers(self):
        resp = await self.client.get("/api/me")
        self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Strict-Transport-Security", resp.headers)  # not over HTTPS here
        with mock.patch.dict("os.environ", {"WEB_BASE_URL": "https://dash.example"}):
            resp = await self.client.get("/api/me")
            self.assertIn("Strict-Transport-Security", resp.headers)

    async def test_login_attempts_are_limited(self):
        with mock.patch.dict("os.environ", {"DISCORD_CLIENT_SECRET": "x"}):
            for _ in range(3):
                resp = await self.client.get("/auth/login", allow_redirects=False)
                self.assertEqual(resp.status, 302)
                self.assertIn("Content-Security-Policy", resp.headers)
            resp = await self.client.get("/auth/login", allow_redirects=False)
            self.assertEqual(resp.status, 429)


if __name__ == "__main__":
    unittest.main()
