"""cogs/roster.py: the guild roster kept posted in Discord."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import config
import roster
from cogs import roster as roster_cog

REF = {"classes": [{"id": 5, "name": "Priest", "specs": []}]}


class FakeChannel:
    def __init__(self):
        self.id, self.name = 700, "guild-roster"
        self.sent, self.edited, self.deleted = [], [], []
        member = SimpleNamespace(mention="<@111>")
        self.guild = SimpleNamespace(name="Test", get_member=lambda i: member if i == 111 else None,
                                     emojis=[])
        self.missing = set()

    async def send(self, view=None, allowed_mentions=None):
        self.sent.append(view)
        return SimpleNamespace(id=9000 + len(self.sent))

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def edit(self, view=None, allowed_mentions=None):
                if message_id in channel.missing:
                    raise discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Message")
                channel.edited.append(message_id)

            async def delete(self):
                channel.deleted.append(message_id)

        return Partial()


def text_of(view) -> str:
    return "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))


class RosterPostTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel()
        self._patches = [mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                             GUILD_ROSTER_GUILD="Encore", GUILD_ROSTER_CHANNEL_ID=700),
                         mock.patch.object(roster_cog, "reference", mock.AsyncMock(return_value=REF)),
                         mock.patch.object(roster_cog.icons, "resolve_class_icon", lambda g, n: "<:priest:1>")]
        for p in self._patches:
            p.start()
        self.cog = roster_cog.RosterCog.__new__(roster_cog.RosterCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda cid: self.channel if cid == 700 else None)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    async def test_posts_then_edits_only_on_change(self):
        roster.store_roster([char(1, 15), char(2, 12, name="Jaina", surname="Proud")], "encore")
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=True)
        self.assertIn("Posted", await self.cog.publish_roster())
        text = text_of(self.channel.sent[0])
        self.assertIn("## Encore", text)
        self.assertIn("2 characters", text)
        self.assertIn("`15` <:priest:1> **Eggmike Thewhite** · <@111>", text)
        self.assertLess(text.index("Eggmike"), text.index("Jaina"))  # highest level first
        self.assertIsNone(await self.cog.publish_roster())  # nothing changed: no edit
        self.assertEqual(self.channel.edited, [])
        roster.store_roster([char(1, 16), char(2, 12, name="Jaina", surname="Proud")], "encore")
        await self.cog.publish_roster()
        self.assertEqual(self.channel.edited, [9001])  # edited in place
        self.assertEqual(len(self.channel.sent), 1)

    async def test_deleted_message_is_posted_again(self):
        roster.store_roster([char(1, 15)], "encore")
        await self.cog.publish_roster()
        self.channel.missing.add(9001)
        await self.cog.publish_roster(force=True)
        self.assertEqual(len(self.channel.sent), 2)

    def test_long_rosters_span_messages(self):
        chunks = self.cog._chunks("header", [f"line {i:04d} " + "x" * 80 for i in range(100)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= roster_cog.POST_TEXT_LIMIT for c in chunks))
        self.assertTrue(chunks[0].startswith("header"))

    async def test_no_channel_set(self):
        with mock.patch.object(config, "GUILD_ROSTER_CHANNEL_ID", 0):
            self.assertIsNone(await self.cog.publish_roster())
        self.assertEqual(self.channel.sent, [])


if __name__ == "__main__":
    unittest.main()
