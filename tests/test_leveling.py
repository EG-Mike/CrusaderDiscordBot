"""cogs/leveling.py: posting level milestones."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from tests.helpers import DatabaseTestCase
from tests.test_roster import char

import config
import roster
from cogs import leveling


class AnnounceTests(DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._config = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu", LEVELING_CHANNEL_ID=500,
                                           LEVELING_MILESTONES=[10, 20], LEVELING_ANNOUNCE_ALTS=False)
        self._config.start()
        self.sent = []
        member = SimpleNamespace(mention="<@111>")

        async def send(text, allowed_mentions=None):
            self.sent.append((text, allowed_mentions))

        self.channel = SimpleNamespace(send=send, name="leveling",
                                       guild=SimpleNamespace(get_member=lambda i: member if i == 111 else None))
        bot = SimpleNamespace(get_channel=lambda cid: self.channel if cid == 500 else None, blizzard=None)
        self.cog = leveling.LevelingCog.__new__(leveling.LevelingCog)
        self.cog.bot = bot
        self._ref = mock.patch.object(leveling, "reference",
                                      mock.AsyncMock(return_value={"classes": [{"id": 5, "name": "Priest", "specs": []}]}))
        self._ref.start()

    def tearDown(self):
        self._ref.stop()
        self._config.stop()
        super().tearDown()

    async def test_milestone_is_posted_once(self):
        roster.store_roster([char(1, 19)], "encore")
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=False)
        roster.store_profile(char(1, 21, guild_rank=None))
        self.assertEqual(await self.cog.announce_pending(), 1)
        text, mentions = self.sent[0]
        self.assertIn("<@111>", text)
        self.assertIn("**Eggmike Thewhite** just reached level **21**", text)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual(await self.cog.announce_pending(), 0)  # not again
        self.assertEqual(roster.recent_events()[0]["outcome"], "announced")

    async def test_alts_and_unlinked_characters(self):
        roster.store_roster([char(1, 15), char(2, 9, name="Jaina", surname="Proud"),
                             char(3, 19, name="Nobody", surname="Linked")], "encore")
        roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=False)
        roster.link("jaina-proud", 111, by="Mike", as_moderator=False)  # an alt
        roster.store_roster([char(1, 15), char(2, 10, name="Jaina", surname="Proud"),
                             char(3, 20, name="Nobody", surname="Linked")], "encore")
        self.assertEqual(await self.cog.announce_pending(), 1)
        # The unlinked character's first name stands in for the mention.
        self.assertIn("Congratulations Nobody! **Nobody Linked**", self.sent[0][0])
        self.assertEqual({e["outcome"] for e in roster.recent_events()}, {"announced", "alt"})

    async def test_failed_post_is_retried(self):
        roster.store_roster([char(1, 9)], "encore")
        roster.store_roster([char(1, 10)], "encore")

        async def broken(*_args, **_kwargs):
            raise discord.HTTPException(mock.Mock(status=403, reason="Forbidden"), "Missing Access")

        self.channel.send = broken
        with self.assertLogs("wow-apply-bot.leveling", "WARNING"):
            self.assertEqual(await self.cog.announce_pending(), 0)
        self.assertEqual(len(roster.pending_announcements()), 1)


if __name__ == "__main__":
    unittest.main()
