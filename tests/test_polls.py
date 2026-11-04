"""cogs/polls.py's shared rules and the dashboard polls API."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import storage
from cogs import polls
from webapi import auth, server


def record(**settings):
    return polls.build_record("Raid night?", None, ["Wed", "Thu", "Sun"], settings, 1, "Mike", 500)


class BuildRecordTests(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        r = polls.build_record("  Raid   night? ", "  extra  ", ["Wed", " wed ", "", "Thu"], {}, 1, "Mike", 500)
        self.assertEqual(r["question"], "Raid night?")
        self.assertEqual(r["description"], "extra")
        self.assertEqual([o["text"] for o in r["options"]], ["Wed", "Thu"])  # duplicate dropped
        self.assertEqual(r["phase"], polls.PHASE_VOTING)
        self.assertFalse(r["members_can_add"])

    def test_member_options_start_with_adding(self):
        r = polls.build_record("Guild name?", None, [], {}, 1, "Mike", 500)
        self.assertTrue(r["members_can_add"])
        self.assertEqual(r["phase"], polls.PHASE_ADDING)

    def test_duration_sets_deadline_when_voting_starts(self):
        r = record(duration="2d")
        self.assertEqual(r["duration"], 2 * 86400)
        self.assertIsNotNone(r["closes_at"])

    def test_validation(self):
        for args, message in [
            (("", None, ["a", "b"], {}), "question"),
            (("Q", None, ["only one"], {}), "at least 2"),
            (("Q", None, ["x" * 60, "b"], {}), "too long"),
            (("Q", None, ["a", "b"], {"duration": "soon"}), "duration"),
            (("Q", None, ["a", "b"], {"duration": "40d"}), "30 days"),
            (("Q", None, ["a", "b"], {"max_votes": 99}), "between 1 and 25"),
        ]:
            with self.assertRaisesRegex(polls.PollError, message):
                polls.build_record(*args, 1, "Mike", 500)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.cog = polls.PollsCog.__new__(polls.PollsCog)  # rules only; no Discord needed

    def test_phase_changes(self):
        r = record()
        self.assertIsNone(self.cog._phase_change(r, "close"))
        self.assertEqual(r["phase"], polls.PHASE_CLOSED)
        self.assertIn("already closed", self.cog._phase_change(r, "close"))
        self.assertIsNone(self.cog._phase_change(r, "reopen_voting"))
        self.assertIn("no deadline", self.cog._phase_change(r, "clear_deadline"))
        self.assertEqual(self.cog._phase_change(r, "explode"), "Unknown action.")

    def test_open_voting_needs_two_options(self):
        r = polls.build_record("Guild name?", None, [], {}, 1, "Mike", 500)
        self.assertIn("at least 2", self.cog._phase_change(r, "open_voting"))
        self.cog._insert_option(r, "Crusade", 2, "Jaina", as_manager=True)
        self.cog._insert_option(r, "Light", 3, "Thrall", as_manager=True)
        self.assertIsNone(self.cog._phase_change(r, "open_voting"))
        self.assertEqual(r["phase"], polls.PHASE_VOTING)

    def test_privacy_toggles_are_one_way(self):
        r = record(show_voters=True)
        self.assertIsNone(self.cog._set_toggle(r, "show_voters", False))
        self.assertIn("can't be turned back on", self.cog._set_toggle(r, "show_voters", True))
        self.assertFalse(r["show_voters"])
        self.assertIsNone(self.cog._set_toggle(r, "live_results", True))  # normal toggle, both ways

    def test_options(self):
        r = record()
        self.assertIn("already an option", self.cog._insert_option(r, "wed", 2, "Jaina", as_manager=True))
        self.assertIsNone(self.cog._insert_option(r, "Fri", 2, "Jaina", as_manager=True))
        r["votes"] = {"10": [1], "11": [1, 2]}
        removed = self.cog._drop_options(r, {1})
        self.assertEqual([o["text"] for o in removed], ["Wed"])
        self.assertEqual(r["votes"], {"11": [2]})  # ballots for removed options are dropped


def fake_channel():
    channel = mock.MagicMock(spec=discord.TextChannel)
    channel.id, channel.name = 500, "raid-chat"
    channel.guild.me = object()
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
    message = mock.MagicMock()
    message.id = 9001
    channel.send = mock.AsyncMock(return_value=message)
    partial = mock.MagicMock()
    partial.id = 9001
    partial.edit = mock.AsyncMock()
    partial.reply = mock.AsyncMock()
    partial.delete = mock.AsyncMock()
    channel.get_partial_message.return_value = partial
    return channel, partial


class PollApiTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fake_levels(_bot, _user_id, fresh=False):
            return {"moderator"}

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self._folder = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._folder.name)  # polls.json is written to the working directory
        self.channel, self.message = fake_channel()
        cogs = {}
        self.bot = SimpleNamespace(guilds=[], latency=0.05, is_ready=lambda: True, is_closed=lambda: False,
                                   user=None, store=SimpleNamespace(items=lambda: []),
                                   get_channel=lambda cid: self.channel if cid == 500 else None,
                                   get_cog=lambda name: cogs.get(name))
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "json"}):
            cogs["PollsCog"] = polls.PollsCog(self.bot)
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()
        storage.REGISTRY.pop("polls", None)
        os.chdir(self._cwd)
        self._folder.cleanup()

    async def call(self, method, path, body=None):
        resp = await self.client.request(method, path, json=body, headers=self.headers)
        return resp.status, await resp.json()

    async def test_create_vote_privacy_and_close(self):
        status, poll = await self.call("POST", "/api/polls", {
            "question": "Raid night?", "options": ["Wed", "Thu"], "channel_id": "500",
            "settings": {"show_voters": False, "duration": "1d"}})
        self.assertEqual(status, 201)
        self.assertEqual((poll["phase"], poll["channel_name"]), ("voting", "raid-chat"))
        self.assertIsInstance(self.channel.send.call_args.kwargs["view"], discord.ui.LayoutView)
        pid = poll["message_id"]

        cog = self.bot.get_cog("PollsCog")
        r = cog._get(int(pid))
        r["votes"] = {"42": [1]}
        cog._save(int(pid), r)
        status, poll = await self.call("GET", f"/api/polls/{pid}")
        self.assertEqual(poll["options"][0]["votes"], 1)
        self.assertEqual(poll["options"][0]["voters"], [])  # anonymous poll: counts only

        status, poll = await self.call("POST", f"/api/polls/{pid}/action", {"action": "add_option", "text": "Sun"})
        self.assertEqual([o["text"] for o in poll["options"]], ["Wed", "Thu", "Sun"])
        self.assertTrue(self.message.edit.called)  # the Discord message was re-rendered
        status, body = await self.call("POST", f"/api/polls/{pid}/action", {"action": "toggle", "key": "show_voters",
                                                                            "value": True})
        self.assertEqual(status, 400)
        self.assertIn("can't be turned back on", body["error"])

        status, poll = await self.call("POST", f"/api/polls/{pid}/action", {"action": "close"})
        self.assertEqual(poll["phase"], "closed")
        self.assertIn("Wed", self.message.reply.call_args.args[0])  # the result was announced

        import audit
        self.assertIn("Closed the poll “Raid night?”", [e["summary"] for e in audit.search()[0]])

        status, _ = await self.call("DELETE", f"/api/polls/{pid}")
        self.assertEqual(status, 200)
        self.assertTrue(self.message.delete.called)
        status, body = await self.call("GET", "/api/polls")
        self.assertEqual(body["polls"], [])

    async def test_errors(self):
        status, body = await self.call("POST", "/api/polls", {"question": "Q", "options": ["a"], "channel_id": "500"})
        self.assertEqual(status, 400)
        status, body = await self.call("POST", "/api/polls", {"question": "Q", "options": ["a", "b"], "channel_id": "7"})
        self.assertIn("text channel", body["error"])
        status, _ = await self.call("POST", "/api/polls/123/action", {"action": "close"})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", "/api/polls/123/action", {"action": "hack"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
