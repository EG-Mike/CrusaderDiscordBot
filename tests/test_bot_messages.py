"""bot_messages.py and its dashboard API."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_audit_api import _fake_bot

import bot_messages as bm
from webapi import auth, server


class TextTests(DatabaseTestCase):
    def test_fill_only_replaces_known_names(self):
        self.assertEqual(bm.fill("Hi {character}, {unknown} {0.__class__}", {"character": "Thrall"}),
                         "Hi Thrall, {unknown} {0.__class__}")

    def test_defaults_and_settings_placeholders(self):
        self.assertFalse(bm.current("approval_dm")["customized"])
        self.assertIn("**Thrall**", bm.text("approval_dm", character="Thrall"))
        with mock.patch.object(bm.config, "ATTENDANCE_MIN_ATTENDED", 7):
            self.assertIn("`7`", bm.render("attendance_explainer")["body"])

    def test_embed_has_marker_and_colour(self):
        embed = bm.embed("review_explainer")
        self.assertEqual(embed.footer.text, "review-channel-explainer")
        self.assertEqual(embed.color.value, 0x5865F2)
        self.assertIn("✅ Approve", embed.description)

    def test_save_reset_and_history(self):
        bm.save("denial_dm", None, "Sorry {character}.", None, "Mike")
        self.assertEqual(bm.text("denial_dm", character="Jaina"), "Sorry Jaina.")
        self.assertTrue(bm.current("denial_dm")["customized"])
        bm.reset("denial_dm", "Mike")
        self.assertFalse(bm.current("denial_dm")["customized"])
        history = bm.history("denial_dm")
        self.assertEqual(len(history), 2)
        self.assertIn("reset", history[0]["saved_by"])

    def test_saving_the_default_text_is_a_reset(self):
        d = bm.definition("denial_dm")
        bm.save("denial_dm", None, d.default_body, None, "Mike")
        self.assertFalse(bm.current("denial_dm")["customized"])

    def test_validation(self):
        with self.assertRaises(bm.BotMessageError):
            bm.save("denial_dm", None, "   ", None, "Mike")
        with self.assertRaises(bm.BotMessageError):
            bm.save("denial_dm", None, "x" * 2000, None, "Mike")
        with self.assertRaises(bm.BotMessageError):
            bm.save("review_explainer", "t", "body", "green", "Mike")
        with self.assertRaises(bm.BotMessageError):
            bm.current("nope")
        # Text messages have no title or colour.
        bm.save("denial_dm", "ignored", "Fine", "#FFFFFF", "Mike")
        self.assertEqual((bm.current("denial_dm")["title"], bm.current("denial_dm")["color"]), (None, None))

    def test_unknown_placeholders(self):
        self.assertEqual(bm.unknown_placeholders("approval_dm", "{character} {charcter} {charcter}"), ["charcter"])


def pinned_channel(pins):
    channel = mock.MagicMock()
    channel.name = "gear-check"
    channel.pins = mock.AsyncMock(return_value=pins)
    posted = mock.MagicMock()
    posted.pin = mock.AsyncMock()
    channel.send = mock.AsyncMock(return_value=posted)
    return channel, posted


def pinned_message(embed):
    message = mock.MagicMock()
    message.embeds = [embed]
    message.edit = mock.AsyncMock()
    return message


class SyncPinnedTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_message_is_left_alone(self):
        existing = pinned_message(bm.embed("gear_check_explainer"))
        channel, _ = pinned_channel([existing])
        self.assertIn("up to date", await bm.sync_pinned(channel, "gear_check_explainer"))
        existing.edit.assert_not_called()

    async def test_changed_text_edits_in_place(self):
        existing = pinned_message(bm.embed("gear_check_explainer"))
        bm.save("gear_check_explainer", "New title", "New text", None, "Mike")
        channel, _ = pinned_channel([existing])
        self.assertIn("Updated", await bm.sync_pinned(channel, "gear_check_explainer"))
        self.assertEqual(existing.edit.call_args.kwargs["embed"].title, "New title")
        channel.send.assert_not_called()

    async def test_missing_message_is_posted_and_pinned(self):
        channel, posted = pinned_channel([])
        seen = []
        result = await bm.sync_pinned(channel, "attendance_explainer", on_posted=seen.append)
        self.assertIn("Posted and pinned", result)
        posted.pin.assert_awaited()
        self.assertEqual(seen, [posted])

    async def test_pins_as_async_iterator(self):
        """Newer discord.py: channel.pins() is iterated with `async for`."""
        existing = pinned_message(bm.embed("gear_check_explainer"))

        class Pins:
            def __aiter__(self):
                async def gen():
                    yield existing
                return gen()

        channel, _ = pinned_channel([])
        channel.pins = mock.MagicMock(return_value=Pins())
        self.assertIn("up to date", await bm.sync_pinned(channel, "gear_check_explainer"))

    async def test_unusable_channel(self):
        with self.assertRaises(bm.BotMessageError):
            await bm.sync_pinned(None, "review_explainer")
        channel, _ = pinned_channel([])
        channel.pins.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        with self.assertRaisesRegex(bm.BotMessageError, "pinned"):
            await bm.sync_pinned(channel, "review_explainer")

    async def test_update_without_updater(self):
        bm._UPDATERS.clear()
        with self.assertRaisesRegex(bm.BotMessageError, "isn't managing"):
            await bm.update_in_discord("review_explainer")
        with self.assertRaisesRegex(bm.BotMessageError, "sent when needed"):
            await bm.update_in_discord("approval_dm")


class BotMessagesApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fake_levels(_bot, _user_id, fresh=False):
            return {"moderator"}

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self.client = TestClient(TestServer(server.create_app(_fake_bot())))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}
        bm._UPDATERS.clear()

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()
        bm._UPDATERS.clear()

    async def test_list(self):
        body = await (await self.client.get("/api/bot-messages")).json()
        self.assertEqual(len(body), len(bm.DEFINITIONS))
        approval = next(m for m in body if m["key"] == "approval_dm")
        self.assertEqual(approval["variables"][0]["name"], "character")
        self.assertFalse(approval["customized"])

    async def test_save_with_discord_update_and_reset(self):
        calls = []

        async def updater():
            calls.append(1)
            return "Updated the pinned message in #review."

        bm.register_updater("review_explainer", updater)
        resp = await self.client.put("/api/bot-messages/review_explainer?update_discord=1", headers=self.headers,
                                     json={"title": "How we review", "body": "Click {approve_emoji} or {oops}"})
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["discord"], "Updated the pinned message in #review.")
        self.assertEqual(body["unknown_placeholders"], ["oops"])
        self.assertTrue(body["message"]["customized"])
        self.assertEqual(calls, [1])

        resp = await self.client.delete("/api/bot-messages/review_explainer", headers=self.headers)
        self.assertFalse((await resp.json())["message"]["customized"])

        import audit
        summaries = [e["summary"] for e in audit.search()[0]]
        self.assertIn("Edited the bot message “Review channel explainer”", summaries)
        self.assertFalse(any("Click" in s for s in summaries))

    async def test_discord_failure_is_a_note_not_an_error(self):
        resp = await self.client.put("/api/bot-messages/review_explainer?update_discord=1", headers=self.headers,
                                     json={"title": "t", "body": "b"})
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertIn("isn't managing", body["discord_error"])

    async def test_errors(self):
        resp = await self.client.put("/api/bot-messages/nope", headers=self.headers, json={"body": "x"})
        self.assertEqual(resp.status, 404)
        resp = await self.client.put("/api/bot-messages/denial_dm", headers=self.headers, json={"body": ""})
        self.assertEqual(resp.status, 400)
        resp = await self.client.put("/api/bot-messages/denial_dm", json={"body": "x"})  # no CSRF header
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
