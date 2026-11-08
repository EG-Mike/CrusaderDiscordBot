"""Links waiting for their character, and professions on the posted roster."""

import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import DatabaseTestCase
from tests.test_roster import char
from tests.test_roster_post import REF, FakeChannel, text_of

import config
import roster
import wow_api
from cogs import apply
from cogs import roster as roster_cog

PROFESSIONS = {"primaries": [
    {"profession": {"name": "Enchanting", "id": 333}, "tiers": [{"skill_points": 200, "max_skill_points": 225}]},
    {"profession": {"name": "Tailoring", "id": 197}, "tiers": [{"skill_points": 45, "max_skill_points": 75}]}],
    "secondaries": [{"profession": {"name": "Cooking", "id": 185},
                     "tiers": [{"skill_points": 16, "max_skill_points": 75}]}]}


class Scoped(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._scope = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                          GUILD_ROSTER_GUILD="Encore", GUILD_ROSTER_CHANNEL_ID=700)
        self._scope.start()

    def tearDown(self):
        self._scope.stop()
        super().tearDown()


class PendingLinkTests(Scoped):
    def test_link_waits_until_the_character_shows_up(self):
        roster.remember_link("eggmike-thewhite", 111, by="application approval", main_spec_id=78, off_spec_id=56)
        self.assertEqual(roster.apply_pending_links(), [])  # not on the roster yet: keeps waiting
        roster.store_roster([char(1, 8)], "encore")          # joins the guild at level 8
        c = roster.get("eggmike-thewhite")
        self.assertEqual((c["discord_id"], c["is_main"], c["main_spec_id"], c["off_spec_id"]), (111, 1, 78, 56))
        self.assertEqual(roster.pending_links(), [])

    def test_a_profile_also_applies_it(self):
        roster.remember_link("eggmike-thewhite", 111, by="application approval")
        roster.store_profile(char(1, 10, guild_slug=None, guild_rank=None))
        self.assertEqual(roster.get("eggmike-thewhite")["discord_id"], 111)

    def test_someone_elses_link_wins(self):
        roster.store_roster([char(1, 12)], "encore")
        roster.link("eggmike-thewhite", 222, by="Mike", as_moderator=True)
        roster.remember_link("eggmike-thewhite", 111, by="application approval")
        roster.apply_pending_links()
        self.assertEqual(roster.get("eggmike-thewhite")["discord_id"], 222)
        self.assertEqual(roster.pending_links(), [])


class ApprovalLinkTests(Scoped, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_character_leaves_a_pending_link(self):
        cog = apply.ApplyCog.__new__(apply.ApplyCog)
        cog.bot = SimpleNamespace(get_cog=lambda name: object() if name == "RosterCog" else None)
        record = {"applicant_id": 111, "character_slug": "eggmike-thewhite", "spec_id": 78, "off_spec_id": None}
        with mock.patch.object(roster, "find_or_add", mock.AsyncMock(side_effect=roster.RosterError("not found"))):
            line = await cog._link_on_roster(record)
        self.assertIn("will be linked as soon as Blizzard shows the character", line)
        [pending] = roster.pending_links()
        self.assertEqual((pending["slug"], pending["discord_id"], pending["main_spec_id"]), ("eggmike-thewhite", 111, 78))


class ProfessionTests(Scoped, unittest.IsolatedAsyncioTestCase):
    def test_primary_professions_are_stored(self):
        roster.store_roster([char(1, 30)], "encore")
        roster.store_professions("eggmike-thewhite", PROFESSIONS)
        stored = roster.get("eggmike-thewhite")
        self.assertEqual(roster.professions(stored, primary_only=True),
                         [{"id": 333, "name": "Enchanting", "skill": 200, "max": 225, "primary": True},
                          {"id": 197, "name": "Tailoring", "skill": 45, "max": 75, "primary": True}])
        self.assertEqual([p["name"] for p in roster.professions(stored)], ["Enchanting", "Tailoring", "Cooking"])
        older = {"professions": '[{"id": 333, "name": "Enchanting", "skill": 1, "max": 75}]'}  # before secondaries
        self.assertTrue(roster.professions(older, primary_only=True)[0]["primary"])
        self.assertEqual(roster.professions({"professions": "not json"}), [])

    async def test_read_only_after_a_logout(self):
        roster.store_roster([char(1, 30, last_login=1000)], "encore")
        profile = mock.AsyncMock(return_value=char(1, 30, last_login=1000, guild_rank=None))
        read = mock.AsyncMock(return_value=PROFESSIONS)
        bot = SimpleNamespace()
        with mock.patch.object(wow_api, "character_profile", profile), \
                mock.patch.object(wow_api, "character_professions", read):
            await roster.refresh_profiles(bot, roster.characters())  # never read: read now
            await roster.refresh_profiles(bot, roster.characters())  # same login: not again
            profile.return_value = char(1, 31, last_login=2000, guild_rank=None)
            await roster.refresh_profiles(bot, roster.characters())  # logged out since: read again
        self.assertEqual(read.await_count, 2)

    async def test_posted_roster_shows_icons_and_skill(self):
        channel = FakeChannel()
        cog = roster_cog.RosterCog.__new__(roster_cog.RosterCog)
        cog.bot = SimpleNamespace(get_channel=lambda cid: channel if cid == 700 else None)
        cog._post_lock = roster_cog.asyncio.Lock()
        roster.store_roster([char(1, 30)], "encore")
        roster.store_professions("eggmike-thewhite", PROFESSIONS)
        icon = mock.AsyncMock(side_effect=lambda bot, kind, pid, by=None: {333: "<:ench:1>"}.get(pid, ""))
        with mock.patch.object(roster_cog, "reference", mock.AsyncMock(return_value=REF)), \
                mock.patch.object(roster_cog.icons, "resolve_class_icon", lambda g, n: "<:priest:1>"), \
                mock.patch.object(roster_cog.emoji_library, "ensure_icon", icon):
            await cog.publish_roster()
            text = text_of(channel.sent[0])
            # Fixed-width cells before the member: a profession without an emoji keeps its place with a blank one.
            self.assertIn("`30` <:priest:1> `Eggmike Thewhite` <:ench:1> `200/225` ▫️ ` 45/ 75` -", text)
            self.assertIn("`Lv` ⚔️ `Character       ` ▫️ `Prof. 1` ▫️ `Prof. 2` **Discord**", text)
            self.assertNotIn("Cooking", text)
            with mock.patch.object(config, "GUILD_ROSTER_POST_PROFESSIONS", False):
                self.assertEqual(await cog._profession_icons(roster.characters()), {})
                lines = cog._post_lines(roster.characters(), REF, channel.guild, {333: "<:ench:1>"})
        self.assertEqual([c.args[1] for c in icon.await_args_list], ["profession", "profession"])
        self.assertNotIn("200/225", "\n".join(lines))
        self.assertNotIn("Prof.", lines[0])

    def test_rows_with_fewer_professions_are_padded(self):
        cog = roster_cog.RosterCog.__new__(roster_cog.RosterCog)
        roster.store_roster([char(1, 30), char(2, 20, name="Jaina", surname="Proud")], "encore")
        roster.store_professions("eggmike-thewhite", PROFESSIONS)
        with mock.patch.object(roster_cog.icons, "resolve_class_icon", lambda g, n: ""), \
                mock.patch.object(roster_cog.icons, "blank", lambda: "<:blank:9>"):
            lines = cog._post_lines(roster.characters(), REF, None, {333: "<:ench:1>", 197: "<:tail:2>"})
        self.assertEqual(lines[1], "`30` <:blank:9> `Eggmike Thewhite` <:ench:1> `200/225` <:tail:2> ` 45/ 75` -")
        self.assertEqual(lines[2], "`20` <:blank:9> `Jaina Proud     ` <:blank:9> `   -   ` <:blank:9> `   -   ` -")


if __name__ == "__main__":
    unittest.main()
