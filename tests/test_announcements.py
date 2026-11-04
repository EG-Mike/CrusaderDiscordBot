"""announcements.py and its dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import announcements as service
import config
from webapi import auth, server


def fake_channel(channel_id=500, *, can_send=True, mention_everyone=False, name="news"):
    """A text channel whose send/fetch/edit/delete are recorded."""
    channel = mock.MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = name
    perms = SimpleNamespace(view_channel=can_send, send_messages=can_send, mention_everyone=mention_everyone)
    channel.permissions_for.return_value = perms
    channel.guild.me = object()
    channel.guild.get_role.return_value = SimpleNamespace(name="Raiders", mentionable=True)
    channel.guild.get_channel.side_effect = lambda cid: channel if cid == channel_id else None
    message = mock.MagicMock()
    message.id = 9001
    message.edit = mock.AsyncMock()
    message.delete = mock.AsyncMock()
    channel.send = mock.AsyncMock(return_value=message)
    channel.fetch_message = mock.AsyncMock(return_value=message)
    return channel, message


def fake_bot(*channels):
    by_id = {c.id: c for c in channels}
    return SimpleNamespace(get_channel=lambda cid: by_id.get(int(cid)), guilds=[], latency=0.05,
                           is_ready=lambda: True, is_closed=lambda: False, user=None,
                           store=SimpleNamespace(items=lambda: []))


class FieldTests(DatabaseTestCase):
    def test_clean_fields(self):
        self.assertEqual(service.clean_fields({"title": "  Hi  ", "color": "#abcdef", "ping": "role:123"}),
                         {"title": "Hi", "color": "#ABCDEF", "ping": "role:123"})
        self.assertEqual(service.clean_fields({"title": "", "ping": ""}), {"title": None, "ping": None})
        for bad in ({"title": "x" * 300}, {"color": "blue"}, {"ping": "@everyone"}, {"ping": "role:abc"}):
            with self.assertRaises(service.AnnouncementError):
                service.clean_fields(bad)

    def test_check_publishable(self):
        with self.assertRaises(service.AnnouncementError):
            service.check_publishable({"title": "x", "body": "  "})
        with self.assertRaises(service.AnnouncementError):
            service.check_publishable({"title": None, "body": "x" * service.MAX_TOTAL_TEXT})
        service.check_publishable({"title": None, "body": "fine"})

    def test_sections(self):
        self.assertEqual(service.split_sections("a\n---\nb\n  ---  \n\n---\nc"), ["a", "b", "c"])

    def test_ping_rendering_and_allowed_mentions(self):
        self.assertEqual(service.ping_text("everyone"), "@everyone")
        self.assertEqual(service.ping_text("role:42"), "<@&42>")
        self.assertTrue(service.allowed_mentions("here").everyone)
        none = service.allowed_mentions(None)
        self.assertFalse(none.everyone)
        self.assertFalse(none.users)
        role = service.allowed_mentions("role:42")
        self.assertEqual([r.id for r in role.roles], [42])
        self.assertFalse(role.everyone)


class ServiceTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def test_create_update_and_conflict(self):
        record = service.create({"title": "Raid night", "body": "Hello"}, "Mike")
        self.assertEqual((record["status"], record["revision"]), ("draft", 1))
        record = service.update(record["id"], {"body": "Hello all"}, "Mike", revision=1)
        self.assertEqual(record["revision"], 2)
        with self.assertRaises(service.Conflict) as caught:
            service.update(record["id"], {"body": "stale"}, "Jaina", revision=1)
        self.assertEqual(caught.exception.current["body"], "Hello all")

    async def test_publish_edit_and_versions(self):
        channel, message = fake_channel()
        bot = fake_bot(channel)
        record = service.create({"title": "Raid night", "body": "Hello"}, "Mike")
        published = await service.publish(bot, record["id"], "500", "Mike", revision=record["revision"])
        self.assertEqual((published["status"], published["message_id"], published["channel_id"]),
                         ("published", "9001", "500"))
        sent = channel.send.call_args.kwargs
        self.assertIsInstance(sent["view"], discord.ui.LayoutView)
        self.assertFalse(sent["allowed_mentions"].everyone)

        edited = await service.edit_published(bot, record["id"], {"body": "Changed"}, "Jaina")
        self.assertEqual(edited["body"], "Changed")
        self.assertTrue(message.edit.called)
        self.assertEqual([v["event"] for v in service.versions(record["id"])], ["edited", "published"])

        with self.assertRaises(service.AnnouncementError):
            await service.publish(bot, record["id"], "500", "Mike")  # already published
        with self.assertRaises(service.AnnouncementError):
            service.update(record["id"], {"ping": "everyone"}, "Mike")  # ping is fixed once published

    async def test_failed_discord_edit_changes_nothing(self):
        channel, message = fake_channel()
        bot = fake_bot(channel)
        record = service.create({"body": "Hello"}, "Mike")
        await service.publish(bot, record["id"], "500", "Mike")
        message.edit.side_effect = discord.HTTPException(SimpleNamespace(status=400, reason="Bad"), "nope")
        with self.assertRaises(service.AnnouncementError):
            await service.edit_published(bot, record["id"], {"body": "New"}, "Mike")
        self.assertEqual(service.get(record["id"])["body"], "Hello")

    async def test_permission_checks(self):
        no_send, _ = fake_channel(can_send=False)
        record = service.create({"body": "Hello", "ping": "everyone"}, "Mike")
        with self.assertRaisesRegex(service.AnnouncementError, "can't post"):
            await service.publish(fake_bot(no_send), record["id"], "500", "Mike")
        no_ping, _ = fake_channel()
        with self.assertRaisesRegex(service.AnnouncementError, "Mention Everyone"):
            await service.publish(fake_bot(no_ping), record["id"], "500", "Mike")
        with self.assertRaisesRegex(service.AnnouncementError, "channel"):
            await service.publish(fake_bot(), record["id"], "123", "Mike")

    async def test_delete_everywhere(self):
        channel, message = fake_channel()
        bot = fake_bot(channel)
        record = service.create({"body": "Hello"}, "Mike")
        await service.publish(bot, record["id"], "500", "Mike")
        self.assertEqual(await service.delete_everywhere(bot, record["id"]), [])
        self.assertTrue(message.delete.called)
        with self.assertRaises(service.NotFound):
            service.get(record["id"])

    def test_templates(self):
        template = service.create_template("Weekly", {"title": "Raid week", "body": "Times:", "color": "#DEAC52"}, "Mike")
        self.assertEqual(service.list_templates()[0]["name"], "Weekly")
        service.update_template(template["id"], "Weekly raids", {}, "Mike")
        self.assertEqual(service.get_template(template["id"])["name"], "Weekly raids")
        with self.assertRaises(service.AnnouncementError):
            service.create_template("  ", {}, "Mike")
        service.delete_template(template["id"])
        self.assertEqual(service.list_templates(), [])

    def test_legacy_import(self):
        old = [
            ("100", {"title": "Draft", "body": "d", "is_draft": True, "published": False}),
            ("200", {"title": "Old", "body": "o", "is_draft": True, "published": True, "published_channel_id": 7,
                     "published_message_id": 300, "published_by": "mike"}),
            ("300", {"title": "Old (edited)", "body": "o2", "channel_id": 7, "is_draft": False,
                     "draft_message_id": 200}),
            ("400", {"title": "Ancient", "body": "a", "channel_id": 8, "is_draft": False, "created_by": 5}),
        ]
        self.assertEqual(service.import_legacy(old), 3)
        self.assertEqual(service.import_legacy(old), 0)  # only once
        self.assertEqual(service.find_by_message(100)["status"], "draft")
        linked = service.find_by_message(200)
        self.assertEqual(linked["id"], service.find_by_message(300)["id"])
        self.assertEqual((linked["status"], linked["body"], linked["channel_id"]), ("published", "o2", "7"))
        self.assertEqual(service.find_by_message(400)["channel_id"], "8")


class AnnouncementApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fake_levels(_bot, _user_id, fresh=False):
            return {"moderator"}

        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.object(config, "FEATURE_ANNOUNCEMENTS_ENABLED", True)]
        for p in self._patches:
            p.start()
        self.channel, self.message = fake_channel()
        self.client = TestClient(TestServer(server.create_app(fake_bot(self.channel))))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def call(self, method, path, body=None):
        resp = await self.client.request(method, path, json=body, headers=self.headers)
        return resp.status, await resp.json()

    async def test_draft_lifecycle(self):
        status, created = await self.call("POST", "/api/announcements", {"title": "Raid night", "body": "Hi"})
        self.assertEqual(status, 201)
        aid = created["id"]
        status, saved = await self.call("PUT", f"/api/announcements/{aid}", {"body": "Hi all", "revision": 1})
        self.assertEqual((status, saved["revision"]), (200, 2))
        status, conflict = await self.call("PUT", f"/api/announcements/{aid}", {"body": "old", "revision": 1})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["current"]["body"], "Hi all")

        status, published = await self.call("POST", f"/api/announcements/{aid}/publish",
                                            {"channel_id": "500", "revision": 2})
        self.assertEqual((status, published["status"], published["channel_name"]), (200, "published", "news"))
        status, listing = await self.call("GET", "/api/announcements?status=published")
        self.assertEqual([a["id"] for a in listing["announcements"]], [aid])
        self.assertEqual(listing["counts"], {"draft": 0, "published": 1})
        status, detail = await self.call("GET", f"/api/announcements/{aid}")
        self.assertEqual(detail["versions"][0]["event"], "published")

        import audit
        summaries = [e["summary"] for e in audit.search()[0]]
        self.assertIn("Published the announcement “Raid night”", summaries)
        self.assertFalse(any("Hi all" in s for s in summaries))  # never the text

    async def test_validation_and_missing(self):
        status, body = await self.call("POST", "/api/announcements", {"color": "red"})
        self.assertEqual(status, 400)
        self.assertIn("colour", body["error"])
        status, _ = await self.call("GET", "/api/announcements/999")
        self.assertEqual(status, 404)
        status, _ = await self.call("GET", "/api/announcements/abc")
        self.assertEqual(status, 404)

    async def test_template_flow(self):
        status, template = await self.call("POST", "/api/announcement-templates",
                                           {"name": "Weekly", "title": "Raid week", "body": "Times"})
        self.assertEqual(status, 201)
        status, created = await self.call("POST", "/api/announcements", {"template_id": template["id"]})
        self.assertEqual((created["title"], created["body"]), ("Raid week", "Times"))

    async def test_feature_off_blocks_changes(self):
        with mock.patch.object(config, "FEATURE_ANNOUNCEMENTS_ENABLED", False):
            status, body = await self.call("POST", "/api/announcements", {"body": "x"})
            self.assertEqual(status, 409)
            status, listing = await self.call("GET", "/api/announcements")
            self.assertFalse(listing["feature_enabled"])


if __name__ == "__main__":
    unittest.main()
