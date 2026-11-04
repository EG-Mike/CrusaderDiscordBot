"""webapi/maintenance_api.py: storage overview, backups and tools, admin-only actions."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import audit
import storage
from webapi import auth, server


class MaintenanceApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator", "admin"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self.synced = []

        async def sync(guild):
            self.synced.append(guild)
            return ["a", "b"]

        guild = SimpleNamespace(id=1, name="Test server", get_member=lambda user_id: None)
        self.bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                                   user=None, store=SimpleNamespace(items=lambda: []), get_channel=lambda cid: None,
                                   get_cog=lambda name: None,
                                   tree=SimpleNamespace(copy_global_to=lambda guild: None, sync=sync))
        os.environ.pop("DISCORD_GUILD_ID", None)
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        self.token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: self.token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()
        storage.REGISTRY.pop("blizzard_item_cache", None)

    async def run_tool(self, action, body=None):
        resp = await self.client.post(f"/api/maintenance/{action}", headers=self.headers, json=body or {})
        return resp.status, await resp.json()

    async def test_overview(self):
        body = await (await self.client.get("/api/maintenance")).json()
        self.assertTrue(body["can_run"])
        self.assertIn("audit_events", [t["table"] for t in body["tables"]])
        self.assertGreater(body["database"]["size"], 0)
        self.levels = {"moderator"}
        body = await (await self.client.get("/api/maintenance")).json()
        self.assertFalse(body["can_run"])

    async def test_backup_is_admin_only_and_a_real_database(self):
        resp = await self.client.get("/api/maintenance/backup")
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertTrue((await resp.read()).startswith(b"SQLite format 3"))
        self.assertEqual(audit.search(limit=1)[0][0]["action"], "maintenance.backup")
        self.levels = {"moderator"}
        self.assertEqual((await self.client.get("/api/maintenance/backup")).status, 403)

    async def test_tools(self):
        status, body = await self.run_tool("apply-retention")
        self.assertEqual(status, 200)
        self.assertIn("Removed", body["result"])
        status, body = await self.run_tool("compact")
        self.assertIn("Compacted", body["result"])
        status, body = await self.run_tool("sync-commands")
        self.assertEqual((status, len(self.synced)), (200, 1))
        self.assertIn("2 slash commands", body["result"])
        status, body = await self.run_tool("legacy-buttons")
        self.assertIn("0 published", body["result"])

    async def test_clear_cache(self):
        store = mock.MagicMock()
        store.items.return_value = [("1", {}), ("2", {})]
        storage.REGISTRY["blizzard_item_cache"] = store
        status, body = await self.run_tool("clear-cache", {"store": "blizzard_item_cache"})
        self.assertEqual(status, 200)
        store.clear.assert_called_once()
        self.assertIn("2 entries", body["result"])
        status, _ = await self.run_tool("clear-cache", {"store": "applications"})  # not a cache
        self.assertEqual(status, 400)

    async def test_logout_others_keeps_me(self):
        other = auth.create_session({"id": "222", "username": "jaina"})
        status, body = await self.run_tool("logout-others")
        self.assertIn("1 other", body["result"])
        self.assertIsNone(auth.get_session(other))
        self.assertIsNotNone(auth.get_session(self.token))

    async def test_moderators_cannot_run_tools(self):
        self.levels = {"moderator"}
        status, body = await self.run_tool("compact")
        self.assertEqual(status, 403)
        status, _ = await self.run_tool("nope")
        self.assertEqual(status, 403)  # refused before even looking the tool up

    async def test_unknown_tool(self):
        status, _ = await self.run_tool("format-disk")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
