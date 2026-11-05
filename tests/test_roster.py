"""roster.py: storing Blizzard's characters, level events and linking."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import DatabaseTestCase

import config
import roster


def char(cid, level, name="Eggmike", surname="Thewhite", **extra):
    slug = f"{name}-{surname}".lower()
    return {"character_id": cid, "slug": slug, "name": name, "surname": surname, "class_id": 5, "race_id": 5,
            "faction": "Horde", "level": level, "ruleset_id": 1, "guild_slug": "encore", "guild_rank": 0,
            "last_login": None, **extra}


class RosterTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self._config = mock.patch.multiple(config, GAME_VERSION="forever", SERVER_REGION="eu",
                                           LEVELING_MILESTONES=[10, 20, 30], LEVELING_ANNOUNCE_ALTS=False)
        self._config.start()

    def tearDown(self):
        self._config.stop()
        super().tearDown()

    def test_first_sighting_is_only_a_baseline(self):
        self.assertEqual(roster.store_roster([char(1, 15)], "encore"), [])
        self.assertEqual(roster.get("eggmike-thewhite")["level"], 15)
        self.assertEqual(roster.recent_events(), [])

    def test_level_ups_and_milestones(self):
        roster.store_roster([char(1, 9)], "encore")
        [event] = roster.store_roster([char(1, 11)], "encore")
        self.assertEqual((event["old_value"], event["new_value"], event["milestone"]), (9, 11, 10))
        self.assertIsNone(roster.store_profile(char(1, 11, guild_rank=None)))  # same level: nothing
        event = roster.store_profile(char(1, 12, guild_rank=None))
        self.assertIsNone(event["milestone"])  # a level-up without a milestone is recorded but not announced
        [pending] = roster.pending_announcements()
        self.assertEqual(pending["milestone"], 10)
        event = roster.store_profile(char(1, 31, guild_rank=None))
        self.assertEqual(event["milestone"], 30)  # jumping past several: only the highest is announced

    def test_levels_never_go_down(self):
        roster.store_roster([char(1, 15)], "encore")
        roster.store_profile(char(1, 16, guild_rank=None))
        roster.store_roster([char(1, 15)], "encore")  # an older roster snapshot
        self.assertEqual(roster.get("eggmike-thewhite")["level"], 16)

    def test_leaving_the_guild(self):
        roster.store_roster([char(1, 15), char(2, 12, name="Jaina", surname="Proud")], "encore")
        roster.store_roster([char(1, 15)], "encore")
        self.assertIsNone(roster.get("jaina-proud")["guild_slug"])
        self.assertEqual(roster.get("eggmike-thewhite")["guild_slug"], "encore")

    def test_linking(self):
        roster.store_roster([char(1, 15), char(2, 12, name="Jaina", surname="Proud")], "encore")
        c = roster.link("eggmike-thewhite", 111, by="Mike", as_moderator=False)
        self.assertEqual((c["discord_id"], c["is_main"]), (111, 1))  # the first character is the main
        c = roster.link("jaina-proud", 111, by="Mike", as_moderator=False)
        self.assertEqual(c["is_main"], 0)
        with self.assertRaisesRegex(roster.RosterError, "already linked to someone else"):
            roster.link("jaina-proud", 222, by="Thrall", as_moderator=False)
        roster.set_main("jaina-proud", discord_id=111, as_moderator=False)
        self.assertEqual([c["slug"] for c in roster.characters(discord_id=111) if c["is_main"]], ["jaina-proud"])
        roster.unlink("jaina-proud", discord_id=111, as_moderator=False)
        self.assertEqual(roster.get("eggmike-thewhite")["is_main"], 1)  # the main moves to what's left
        with self.assertRaisesRegex(roster.RosterError, "isn't yours"):
            roster.unlink("eggmike-thewhite", discord_id=222, as_moderator=False)
        c = roster.link("eggmike-thewhite", 222, by="a moderator", as_moderator=True)
        self.assertEqual(c["discord_id"], 222)

    def test_specs_must_fit_the_class(self):
        roster.store_roster([char(1, 15)], "encore")
        specs = {5: [256, 257, 258]}
        c = roster.set_specs("eggmike-thewhite", 256, 258, discord_id=0, as_moderator=True, class_specs=specs)
        self.assertEqual((c["main_spec_id"], c["off_spec_id"]), (256, 258))
        with self.assertRaisesRegex(roster.RosterError, "own class"):
            roster.set_specs("eggmike-thewhite", 71, None, discord_id=0, as_moderator=True, class_specs=specs)

    def test_who_gets_announced(self):
        now = datetime.now(timezone.utc)
        fresh = now.strftime(roster.TIME_FORMAT)
        self.assertIsNone(roster.should_announce({"created_at": fresh, "discord_id": None}))  # not linked yet
        self.assertIsNone(roster.should_announce({"created_at": fresh, "discord_id": 1, "is_main": 1}))
        self.assertEqual(roster.should_announce({"created_at": fresh, "discord_id": 1, "is_main": 0}), "alt")
        old = (now - timedelta(days=2)).strftime(roster.TIME_FORMAT)
        self.assertEqual(roster.should_announce({"created_at": old, "discord_id": None}), "too old")
        with mock.patch.object(config, "LEVELING_ANNOUNCE_ALTS", True):
            self.assertIsNone(roster.should_announce({"created_at": fresh, "discord_id": 1, "is_main": 0}))

    def test_profile_checks_skip_low_and_idle_characters(self):
        now = datetime.now(timezone.utc)
        long_ago = (now - timedelta(days=10)).timestamp() * 1000
        roster.store_roster([char(1, 15), char(2, 5, name="Low", surname="Level"),
                             char(3, 20, name="Idle", surname="Player", last_login=long_ago)], "encore")
        roster.store_profile(char(3, 20, name="Idle", surname="Player", last_login=long_ago, guild_rank=None))
        due = [c["slug"] for c in roster.due_for_profile_check(now)]
        self.assertEqual(due, ["eggmike-thewhite"])  # level 5: no profile yet; idle: checked a moment ago
        later = now + timedelta(hours=roster.IDLE_CHECK_HOURS + 1)
        self.assertIn("idle-player", [c["slug"] for c in roster.due_for_profile_check(later)])


if __name__ == "__main__":
    unittest.main()
