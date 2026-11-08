"""cogs/notifications.py, its dashboard API, and settings that apply immediately."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import config
import system_log
from cogs import notifications as n
from webapi import auth, server
from webapi import settings as settings_api


def entry(event, created_at, **details):
    return {"event": event, "created_at": created_at, "message": details.pop("message", event),
            "details": details}


class BackOnlineTests(unittest.TestCase):
    def test_plain_restart(self):
        embed = n.back_online_embed([
            entry("bot.stopped", "2026-11-04T10:00:00Z", reason="restarted from the dashboard by Mike"),
            entry("bot.started", "2026-11-04T10:00:30Z", version="abc1234", title="Fix things"),
        ])
        self.assertEqual(embed.title, "🟢 Back online")
        self.assertIn("abc1234", embed.description)
        self.assertIn("Stopped because: restarted from the dashboard by Mike.", embed.description)
        self.assertIn("Offline for about 30 seconds.", embed.description)

    def test_update(self):
        embed = n.back_online_embed([
            entry("bot.stopped", "2026-11-04T10:00:00Z", reason="the host asked it to stop"),
            entry("bot.updated", "2026-11-04T10:03:00Z", previous_version="aaa", compare_url="https://x/compare"),
            entry("bot.started", "2026-11-04T10:03:00Z", version="bbb", title="New stuff"),
        ])
        self.assertEqual(embed.title, "🔄 Updated and back online")
        self.assertIn("**bbb**: New stuff", embed.description)
        self.assertIn("[what changed](https://x/compare)", embed.description)
        self.assertIn("about 3 minutes", embed.description)

    def test_unexpected_stop(self):
        embed = n.back_online_embed([
            entry("bot.crashed", "2026-11-04T11:00:00Z", last_heartbeat="2026-11-04T10:00:00Z"),
            entry("bot.started", "2026-11-04T11:00:00Z", version="bbb"),
        ])
        self.assertEqual(embed.title, "🔴 Back online after an unexpected stop")
        self.assertIn("<t:", embed.description)
        self.assertIn("about 60 minutes", embed.description)

    def test_nothing_to_report(self):
        self.assertIsNone(n.back_online_embed([]))
        self.assertEqual(n.back_online_embed([entry("bot.started", "2026-11-04T11:00:00Z", version="x")]).title,
                         "🟢 Online")

    def test_source_label(self):
        self.assertEqual(n.source_label("wow-apply-bot.raid_summary"), "Raid summary")
        self.assertEqual(n.source_label("discord.client"), "Discord library")


def fake_channel(can_send=True):
    channel = mock.MagicMock()
    channel.name = "mod-chat"
    channel.send = mock.AsyncMock()
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=can_send, embed_links=True)
    return channel


class CogTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.channel = fake_channel()
        self.bot = SimpleNamespace(get_channel=lambda cid: self.channel if cid == 42 else None, guilds=[])
        self.cog = n.NotificationsCog(self.bot)
        self.cog.ready_once = True
        self._patches = [mock.patch.multiple(config, NOTIFICATIONS_CHANNEL_ID=0, MODERATOR_CHANNEL_ID=42,
                                             NOTIFY_BOT_STATUS=True, NOTIFY_ERRORS=True, NOTIFY_ERRORS_EVERY_MINUTES=15,
                                             NOTIFY_DASHBOARD_CHANGES=True, NOTIFY_ANNOUNCEMENTS=True)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def sent(self):
        return [c.kwargs["embed"] for c in self.channel.send.call_args_list]

    def test_destination_falls_back_to_moderator_channel(self):
        self.assertEqual(self.cog.destination()["source"], "moderator")
        with mock.patch.object(config, "MODERATOR_CHANNEL_ID", 0):
            self.assertIn("No channel", self.cog.destination()["problem"])
        with mock.patch.object(config, "NOTIFICATIONS_CHANNEL_ID", 7):
            self.assertIn("can't see", self.cog.destination()["problem"])

    async def test_error_digest_has_counts_not_text(self):
        for _ in range(3):
            self.cog._on_system_entry({"category": "console", "level": "ERROR", "event": "wow-apply-bot.raid_summary",
                                       "details": {"logger": "wow-apply-bot.raid_summary"}, "message": "secret detail"})
        self.cog._on_system_entry({"category": "console", "level": "WARNING", "event": "x", "details": {}, "message": ""})
        self.cog._on_system_entry({"category": "console", "level": "ERROR", "event": n.log.name, "details": {},
                                   "message": "own failure"})
        await self.cog.flush(now=1000.0)
        embed = self.sent()[0]
        self.assertEqual(embed.title, "⚠️ 3 errors in the bot's console")
        self.assertIn("Raid summary: 3", embed.description)
        self.assertNotIn("secret detail", embed.description)
        # Rate limit: another error right after waits for the next interval.
        self.cog._on_system_entry({"category": "console", "level": "ERROR", "event": "a", "details": {}, "message": ""})
        await self.cog.flush(now=1060.0)
        self.assertEqual(len(self.sent()), 1)
        await self.cog.flush(now=1000.0 + 15 * 60)
        self.assertEqual(len(self.sent()), 2)

    async def test_dashboard_changes_are_batched(self):
        def change(kind, action, summary):
            self.cog._on_audit_event({"source": "dashboard", "kind": kind, "action": action, "summary": summary,
                                      "actor_name": "Mike", "details": {}})
        # A fixed clock: with the real one, `last + 30 - last` can come out a hair under 30 when the sum
        # crosses a power of two (e.g. 98.1 + 30 > 128), and the batch wouldn't be due yet.
        with mock.patch.object(n.time, "monotonic", return_value=1000.0):
            change("setting", "settings.update", "Changed setting Region")
            change("setting", "settings.update", "Changed setting Realm")
            change("login", "auth.login", "Logged in")  # not reported
            change("announcement", "announcements.publish", "Published the announcement “Raid”")
            self.cog._on_audit_event({"source": "discord", "kind": "command", "action": "/x", "summary": "",
                                      "details": {}})
        last = self.cog.changes[-1]["_at"]
        self.assertEqual(last, 1000.0)
        await self.cog.flush(now=last + 5)
        self.assertEqual(self.sent(), [])  # still within the quiet period
        await self.cog.flush(now=last + n.CHANGES_QUIET_SECONDS)
        titles = [e.title for e in self.sent()]
        self.assertEqual(titles, ["🛠️ Changes on the dashboard", "📣 Announcements"])
        self.assertIn("**Mike**: Changed setting Realm", self.sent()[0].description)

    async def test_switched_off_kinds_are_dropped(self):
        with mock.patch.multiple(config, NOTIFY_DASHBOARD_CHANGES=False, NOTIFY_ERRORS=False):
            self.cog._on_audit_event({"source": "dashboard", "kind": "setting", "action": "settings.update",
                                      "summary": "x", "actor_name": "Mike", "details": {}})
            self.cog._on_system_entry({"category": "console", "level": "ERROR", "event": "a", "details": {}, "message": ""})
            await self.cog.flush(now=10 ** 6)
        self.assertEqual(self.sent(), [])
        self.assertEqual((self.cog.changes, self.cog.errors), ([], {}))

    async def test_back_online_on_first_ready_only(self):
        system_log.record("INFO", "lifecycle", "bot.stopped", "Bot stopped: x.", {"reason": "x"})
        system_log.record("INFO", "lifecycle", "bot.started", "Bot started", {"version": "abc"})
        self.cog.ready_once = False
        await self.cog.on_ready()
        await self.cog.on_ready()
        self.assertEqual([e.title for e in self.sent()], ["🟢 Back online"])

    async def test_problems_are_warnings_not_errors(self):
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=False,
                                                                    embed_links=True)
        with self.assertLogs(n.log.name, level="WARNING") as logs:
            await self.cog._post_quietly(n.discord.Embed(title="x"))
            await self.cog._post_quietly(n.discord.Embed(title="x"))  # said once
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")


class LiveSettingTests(DatabaseTestCase):
    def test_notification_settings_apply_immediately(self):
        with mock.patch.object(config, "NOTIFY_ERRORS", True):
            result = settings_api.save("NOTIFY_ERRORS", False, "Mike")
            self.assertFalse(config.NOTIFY_ERRORS)
            self.assertFalse(result["pending_restart"])
            self.assertTrue(result["live"])

    def test_other_settings_still_wait_for_a_restart(self):
        result = settings_api.save("LOG_LEVEL", "DEBUG", "Mike")
        self.assertFalse(result["live"])
        self.assertTrue(result["pending_restart"])


class ApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fake_levels(_bot, _user_id, fresh=False):
            return {"moderator"}

        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.multiple(config, NOTIFICATIONS_CHANNEL_ID=42, MODERATOR_CHANNEL_ID=0)]
        for p in self._patches:
            p.start()
        self.channel = fake_channel()
        cogs = {}
        self.bot = SimpleNamespace(guilds=[], latency=0.05, is_ready=lambda: True, is_closed=lambda: False, user=None,
                                   store=SimpleNamespace(items=lambda: []),
                                   get_channel=lambda cid: self.channel if cid == 42 else None,
                                   get_cog=lambda name: cogs.get(name))
        cogs["NotificationsCog"] = n.NotificationsCog(self.bot)
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_status_and_test_message(self):
        body = await (await self.client.get("/api/notifications")).json()
        self.assertEqual((body["channel_name"], body["source"], body["problem"]), ("mod-chat", "notifications", None))
        resp = await self.client.post("/api/notifications/test", headers={server.CSRF_HEADER: "1"})
        self.assertEqual((await resp.json())["channel"], "mod-chat")
        self.assertIsInstance(self.channel.send.call_args.kwargs["embed"], discord.Embed)


if __name__ == "__main__":
    unittest.main()
