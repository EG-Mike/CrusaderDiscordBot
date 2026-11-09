"""cogs/roster.py: the guild roster kept posted in Discord."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import channel_posts
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
                                             GUILD_ROSTER_GUILD="Encore", GUILD_ROSTER_CHANNEL_ID=700,
                                             GUILD_ROSTER_POST_LAYOUT="table"),
                         mock.patch.object(roster_cog, "reference", mock.AsyncMock(return_value=REF)),
                         mock.patch.object(roster_cog.icons, "resolve_class_icon", lambda g, n: "<:priest:1>")]
        for p in self._patches:
            p.start()
        self.cog = roster_cog.RosterCog.__new__(roster_cog.RosterCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda cid: self.channel if cid == 700 else None)
        self.cog._post_lock = roster_cog.asyncio.Lock()

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
        self.assertIn("`Lv` ⚔️ `Character       ` **Discord**", text)  # column headers
        self.assertIn("`15` <:priest:1> `Eggmike Thewhite` <@111>", text)
        self.assertIn("`12` <:priest:1> `Jaina Proud     ` -", text)  # padded to the longest name
        self.assertLess(text.index("Eggmike"), text.index("Jaina"))  # highest level first
        self.assertIsNone(await self.cog.publish_roster())  # nothing changed: no edit
        self.assertEqual(self.channel.edited, [])
        roster.store_roster([char(1, 16), char(2, 12, name="Jaina", surname="Proud")], "encore")
        await self.cog.publish_roster()
        self.assertEqual(self.channel.edited, [9001])  # edited in place
        self.assertEqual(len(self.channel.sent), 1)

    async def test_claim_buttons_on_the_last_message(self):
        roster.store_roster([char(1, 15)], "encore")
        with mock.patch.object(config, "GUILD_ROSTER_SELF_SERVICE", True):
            await self.cog.publish_roster()
        ids = [getattr(i, "custom_id", None) for i in self.channel.sent[0].walk_children()]
        self.assertIn(roster_cog.CLAIM_ID, ids)
        self.assertIn(roster_cog.MINE_ID, ids)
        with mock.patch.object(config, "GUILD_ROSTER_SELF_SERVICE", False):
            await self.cog.publish_roster()  # the setting changed: the post is edited, without "Claim"
        self.assertEqual(self.channel.edited, [9001])

    async def test_low_level_alts_are_left_out(self):
        roster.store_roster([char(1, 15), char(2, 8, name="Jaina", surname="Proud"),
                             char(3, 5, name="Low", surname="Main"), char(4, 7, name="Not", surname="Linked")], "encore")
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=True)
        roster.link("jaina-proud", 111, by="Mike", as_moderator=True)  # an alt below 10
        roster.link("low-main", 222, by="Mike", as_moderator=True)     # a main below 10
        with mock.patch.object(config, "GUILD_ROSTER_POST_MIN_LEVEL", 10):
            names = [c["slug"] for c in roster.posted_characters()]
        self.assertEqual(names, ["eggmike-thewhite", "low-main"])

    async def test_deleted_message_is_posted_again(self):
        roster.store_roster([char(1, 15)], "encore")
        await self.cog.publish_roster()
        self.channel.missing.add(9001)
        await self.cog.publish_roster(force=True)
        self.assertEqual(len(self.channel.sent), 2)

    def test_long_rosters_span_messages(self):
        chunks = channel_posts.chunks("header", [f"line {i:04d} " + "x" * 80 for i in range(100)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= channel_posts.TEXT_LIMIT for c in chunks))
        self.assertTrue(chunks[0].startswith("header"))

    async def test_no_channel_set(self):
        with mock.patch.object(config, "GUILD_ROSTER_CHANNEL_ID", 0):
            self.assertIsNone(await self.cog.publish_roster())
        self.assertEqual(self.channel.sent, [])




class ClaimTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    """The "Claim a character" and "My characters" buttons."""

    def setUp(self):
        super().setUp()
        self._config = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                           GUILD_ROSTER_GUILD="Encore", GUILD_ROSTER_CHANNEL_ID=0,
                                           GUILD_ROSTER_SELF_SERVICE=True, GUILD_ROSTER_POST_MIN_LEVEL=10)
        self._config.start()
        self.cog = roster_cog.RosterCog.__new__(roster_cog.RosterCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda cid: None)
        self.cog._post_lock = roster_cog.asyncio.Lock()
        roster.store_roster([char(1, 15), char(2, 8, name="Jaina", surname="Proud")], "encore")

    def tearDown(self):
        self._config.stop()
        super().tearDown()

    def interaction(self, user_id=111):
        replies = []
        response = SimpleNamespace(is_done=lambda: True, defer=mock.AsyncMock())
        followup = SimpleNamespace(send=mock.AsyncMock(side_effect=lambda text, **kw: replies.append(text)))
        user = SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=False,
                                                                             manage_guild=False), roles=[])
        user.__str__ = lambda self: "Member"
        return SimpleNamespace(user=user, response=response, followup=followup), replies

    async def test_claim_main_then_alt(self):
        interaction, replies = self.interaction()
        await self.cog.claim(interaction, "Eggmike Thewhite", "main")
        self.assertIn("as your main", replies[-1])
        await self.cog.claim(interaction, "Jaina Proud", "alt")
        self.assertIn("as an alt", replies[-1])
        self.assertIn("from level 10", replies[-1])  # a low alt isn't on the posted roster
        mine = {c["slug"]: c["is_main"] for c in roster.characters(discord_id=111)}
        self.assertEqual(mine, {"eggmike-thewhite": 1, "jaina-proud": 0})

    async def test_cannot_claim_someone_elses_character(self):
        roster.link("eggmike-thewhite", 222, by="Mod", as_moderator=True)
        interaction, replies = self.interaction()
        await self.cog.claim(interaction, "Eggmike Thewhite", "main")
        self.assertIn("already linked to someone else", replies[-1])
        self.assertEqual(roster.get("eggmike-thewhite")["discord_id"], 222)

    async def test_claiming_needs_self_service(self):
        interaction, replies = self.interaction()
        with mock.patch.object(config, "GUILD_ROSTER_SELF_SERVICE", False), \
                mock.patch.object(roster_cog, "is_moderator", lambda m: False):
            await self.cog.claim(interaction, "Eggmike Thewhite", "main")
        self.assertIn("Ask a moderator", replies[-1])
        self.assertIsNone(roster.get("eggmike-thewhite")["discord_id"])

    async def test_my_characters_actions(self):
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=False)
        roster.link("jaina-proud", 111, by="Mike", as_moderator=False)
        interaction, _ = self.interaction()
        self.assertIn("is now your main", await self.cog.change_own(interaction, "jaina-proud", "main"))
        self.assertEqual(roster.get("eggmike-thewhite")["is_main"], 0)
        self.assertIn("isn't yours", await self.cog.change_own(self.interaction(222)[0], "jaina-proud", "unlink"))
        self.assertIn("no longer linked", await self.cog.change_own(interaction, "jaina-proud", "unlink"))
        self.assertEqual(roster.get("eggmike-thewhite")["is_main"], 1)  # the other one became the main


if __name__ == "__main__":
    unittest.main()
