"""webapi/server.py: the audit log endpoints, login and access checks."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import audit
from webapi import auth, server


def _fake_bot():
    return SimpleNamespace(guilds=[], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                           user=None, store=SimpleNamespace(items=lambda: []))


class AuditApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self.client = TestClient(TestServer(server.create_app(_fake_bot())))
        await self.client.start_server()
        self.token = auth.create_session({"id": "111", "username": "mike_eg", "global_name": "Mike"})
        self.cookies = {auth.SESSION_COOKIE: self.token}

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()

    async def get(self, path, **kwargs):
        self.client.session.cookie_jar.update_cookies(self.cookies)
        return await self.client.get(path, **kwargs)

    async def test_requires_login(self):
        resp = await self.client.get("/api/audit")
        self.assertEqual(resp.status, 401)

    async def test_requires_moderator(self):
        self.levels = {"approver"}
        resp = await self.get("/api/audit")
        self.assertEqual(resp.status, 403)

    async def test_list_and_filter(self):
        audit.record(source="discord", kind="command", action="/apply", summary="/apply", actor_id=1, actor_name="A")
        audit.record(source="discord", kind="button", action="Approve", summary="Clicked", actor_id=2, actor_name="B")
        body = await (await self.get("/api/audit")).json()
        self.assertEqual([e["action"] for e in body["events"]], ["Approve", "/apply"])
        self.assertFalse(body["has_more"])
        self.assertIsInstance(body["events"][0]["actor_id"], str)

        body = await (await self.get("/api/audit?kind=command")).json()
        self.assertEqual([e["action"] for e in body["events"]], ["/apply"])

        body = await (await self.get("/api/audit?limit=1")).json()
        self.assertTrue(body["has_more"])
        before = body["events"][0]["id"]
        body = await (await self.get(f"/api/audit?before_id={before}")).json()
        self.assertEqual([e["action"] for e in body["events"]], ["/apply"])

    async def test_bad_filters_are_rejected(self):
        for query in ("kind=bogus", "source=x", "actor_id=abc", "since=yesterday", "limit=many"):
            resp = await self.get(f"/api/audit?{query}")
            self.assertEqual(resp.status, 400, query)
            self.assertIn("error", await resp.json())

    async def test_meta(self):
        audit.record(source="discord", kind="command", action="/apply", summary="/apply", actor_id=1, actor_name="A",
                     channel_id=9, channel_name="general")
        body = await (await self.get("/api/audit/meta")).json()
        self.assertEqual(body["actors"][0]["id"], "1")
        self.assertEqual(body["channels"][0]["name"], "general")
        self.assertEqual(body["stats"]["total"], 1)
        self.assertIn("discord_capture_enabled", body)

    async def test_export_is_csv_safe_and_audited(self):
        audit.record(source="discord", kind="command", action="/x", summary="=HYPERLINK(\"evil\")",
                     actor_id=1, actor_name="A")
        resp = await self.get("/api/audit/export")
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        text = await resp.text()
        self.assertIn("'=HYPERLINK", text)
        latest = audit.search(limit=1)[0][0]
        self.assertEqual(latest["action"], "audit.export")
        self.assertEqual(latest["actor_id"], "111")

    async def test_logout_is_audited(self):
        self.client.session.cookie_jar.update_cookies(self.cookies)
        resp = await self.client.post("/auth/logout", headers={server.CSRF_HEADER: "1"})
        self.assertEqual(resp.status, 200)
        latest = audit.search(limit=1)[0][0]
        self.assertEqual((latest["kind"], latest["action"], latest["actor_name"]), ("login", "auth.logout", "Mike"))

    async def test_live_events_reach_moderators_only(self):
        hub = self.client.app[server.HUB_KEY]
        sent = {"mod": [], "viewer": []}

        class FakeWs:
            def __init__(self, name):
                self.name = name

            async def send_str(self, payload):
                sent[self.name].append(payload)

        hub.add(FakeWs("mod"), 1, "t1", {"moderator"})
        hub.add(FakeWs("viewer"), 2, "t2", {"viewer"})
        await hub.broadcast({"type": "audit.event"}, level="moderator")
        self.assertEqual(len(sent["mod"]), 1)
        self.assertEqual(sent["viewer"], [])

    async def test_shutdown_closes_live_connections(self):
        """Open dashboard tabs mustn't hold up a stop (Docker kills after 10 s)."""
        hub = self.client.app[server.HUB_KEY]
        closed = []

        class FakeWs:
            async def close(self, code, message):
                closed.append(code)

        hub.add(FakeWs(), 111, self.token, {"moderator"})
        await server._close_live_connections(self.client.app)
        self.assertEqual(closed, [1001])
        self.assertEqual(hub.sockets, {})

    async def test_revalidation_closes_sockets_after_logout(self):
        hub = self.client.app[server.HUB_KEY]
        closed = []

        class FakeWs:
            async def close(self, code, message):
                closed.append(code)

        hub.add(FakeWs(), 111, self.token, {"moderator"})
        await server._revalidate_sockets(self.client.app)
        self.assertEqual(closed, [])  # still logged in
        auth.delete_session(self.token)
        await server._revalidate_sockets(self.client.app)
        self.assertEqual(closed, [4403])
        self.assertEqual(hub.sockets, {})


if __name__ == "__main__":
    unittest.main()
