"""webapi/branding_api.py: guild logo and name, and the bot's profile."""

import base64
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import config
from webapi import auth, branding_api, server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class BrandingTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(config, "GUILD_DISPLAY_NAME", "")]
        for p in self._patches:
            p.start()
        self.edits = {}

        async def edit_user(**kwargs):
            self.edits.update(kwargs)

        user = SimpleNamespace(name="Crusader", display_avatar=SimpleNamespace(url="https://cdn/avatar.png"),
                               edit=edit_user)
        guild = SimpleNamespace(id=1, name="Test server", icon=None, member_count=3, get_member=lambda _id: None,
                                me=SimpleNamespace(nick=None, guild_permissions=SimpleNamespace(change_nickname=True)))
        bot = SimpleNamespace(guilds=[guild], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                              user=user, store=SimpleNamespace(items=lambda: []), get_guild=lambda _id: guild)
        self.client = TestClient(TestServer(server.create_app(bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_name_falls_back_to_the_server(self):
        body = await (await self.client.get("/branding")).json()
        self.assertEqual((body["name"], body["logo_url"]), ("Test server", None))
        config.GUILD_DISPLAY_NAME = "Crusader"
        body = await (await self.client.get("/branding")).json()
        self.assertEqual(body["name"], "Crusader")

    async def test_logo_upload_checks_the_image(self):
        resp = await self.client.put("/api/branding/logo", json={"data": base64.b64encode(b"<svg>").decode()},
                                     headers=self.headers)
        self.assertEqual(resp.status, 400)
        resp = await self.client.put("/api/branding/logo",
                                     json={"data": "data:image/png;base64," + base64.b64encode(PNG).decode()},
                                     headers=self.headers)
        body = await resp.json()
        self.assertTrue(body["custom_logo"])
        image = await self.client.get(body["logo_url"])
        self.assertEqual((image.status, image.content_type), (200, "image/png"))
        self.assertEqual(await image.read(), PNG)
        resp = await self.client.delete("/api/branding/logo", headers=self.headers)
        self.assertFalse((await resp.json())["custom_logo"])
        self.assertEqual((await self.client.get("/branding/logo")).status, 404)

    async def test_bot_profile_is_for_admins(self):
        body = await (await self.client.get("/api/bot/profile")).json()
        self.assertEqual((body["username"], body["can_edit"]), ("Crusader", False))
        resp = await self.client.put("/api/bot/profile", json={"username": "Crusader Bot"}, headers=self.headers)
        self.assertEqual(resp.status, 403)
        self.levels = {"moderator", "admin"}
        resp = await self.client.put("/api/bot/profile", json={"avatar": "guild_logo"}, headers=self.headers)
        self.assertIn("Upload a guild logo first", (await resp.json())["error"])
        await self.client.put("/api/branding/logo", json={"data": base64.b64encode(PNG).decode()}, headers=self.headers)
        resp = await self.client.put("/api/bot/profile", json={"username": "Crusader Bot", "avatar": "guild_logo"},
                                     headers=self.headers)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.edits, {"username": "Crusader Bot", "avatar": PNG})

    def test_image_type(self):
        self.assertEqual(branding_api.image_type(PNG), "image/png")
        self.assertEqual(branding_api.image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(branding_api.image_type(b"<html>"))


if __name__ == "__main__":
    unittest.main()
