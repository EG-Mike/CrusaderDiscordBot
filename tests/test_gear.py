"""gear.py: summarising Blizzard's gear, talents and profile."""

import unittest
from unittest import mock

import config
import gear

PROFILE = {"name": "Eggmike", "surname": "Thewhite", "level": 60, "character_class": {"id": 5, "name": "Priest"},
           "ruleset": {"id": 2, "name": "Normal"},
           "race": {"name": "Undead"}, "faction": {"name": "Horde"}, "guild": {"name": "Encore", "slug": "encore"},
           "average_item_level": 62, "equipped_item_level": 60, "last_login_timestamp": 1793870276000}


def item(slot, name, ilvl, quality="EPIC", **extra):
    return {"slot": {"type": slot, "name": slot.title()}, "item": {"id": hash(name) % 100000}, "name": name,
            "quality": {"type": quality}, "level": {"value": ilvl}, **extra}


EQUIPMENT = {"equipped_items": [
    item("MAIN_HAND", "Benediction", 70, enchantments=[{"display_string": "Enchanted: +30 Spell Power",
                                                      "enchantment_slot": {"type": "PERMANENT"}}]),
    item("HEAD", "Circlet of Prophecy", 66, set={"item_set": {"name": "Vestments of Prophecy"}},
         sockets=[{"socket_type": {"name": "Meta"}, "item": {"name": "Insightful Earthsiege Diamond"}},
                  {"socket_type": {"name": "Blue"}}]),
    item("SHIRT", "Neophyte's Shirt", 1, quality="COMMON"),
    item("CHEST", "Robes of Prophecy", 66, stats=[{"display": {"display_string": "+20 Intellect"}}]),
]}
SPECS = {"specialization_groups": [
    {"is_active": False, "specializations": [{"specialization": {"name": "Holy", "id": 56}, "spent_points": 41}]},
    {"is_active": True, "specializations": [
        {"specialization": {"name": "Discipline", "id": 613}, "spent_points": 21},
        {"specialization": {"name": "Holy", "id": 56}, "spent_points": 30}]},
]}


class GearTests(unittest.TestCase):
    def setUp(self):
        self._config = mock.patch.multiple(config, GEARCHECK_ENCHANT_SLOTS=["HEAD", "CHEST", "MAIN_HAND"],
                                           GEARCHECK_EXPECTED_SLOTS=["HEAD", "CHEST", "MAIN_HAND", "FINGER_1"],
                                           GEARCHECK_MIN_LEVEL=60, GEARCHECK_MIN_ITEM_LEVEL=65)
        self._config.start()

    def tearDown(self):
        self._config.stop()

    def test_summary(self):
        s = gear.summarise(PROFILE, EQUIPMENT, SPECS)
        self.assertEqual((s["character"], s["slug"], s["guild"], s["equipped_item_level"]),
                         ("Eggmike Thewhite", "eggmike-thewhite", "Encore", 60))
        self.assertEqual([i["slot"] for i in s["items"]], ["HEAD", "CHEST", "SHIRT", "MAIN_HAND"])  # slot order
        head = s["items"][0]
        self.assertEqual(head["set"], "Vestments of Prophecy")
        self.assertEqual([g["gem"] for g in head["sockets"]], ["Insightful Earthsiege Diamond", None])
        self.assertEqual(s["items"][-1]["enchants"], ["Enchanted: +30 Spell Power"])
        # the active talent group only, most points first
        self.assertEqual([(t["spec"], t["points"]) for t in s["talents"]], [("Holy", 30), ("Discipline", 21)])
        self.assertEqual(s["suggested_spec"], "Holy")
        self.assertEqual(s["empty_slots"], ["Ring 1"])
        self.assertEqual(s["missing_enchants"], ["Head", "Chest"])
        self.assertEqual(s["empty_sockets"], 1)
        self.assertEqual(gear.requirement_problems(s), ["item level 60 (needs 65)"])

    def test_render(self):
        s = gear.summarise(PROFILE, EQUIPMENT, SPECS)
        text = gear.render_lines(s, icons={s["items"][0]["item_id"]: "<:inv_helm:1>"})
        self.assertIn("**Item level:** 60 (bags included: 62)", text["overview"])
        self.assertIn("**Talents:** Holy 30 / Discipline 21", text["overview"])
        self.assertIn("⚠️ below on item level 60 (needs 65)", text["overview"])
        self.assertIn("<:inv_helm:1> **Circlet of Prophecy** `66` · Head · Vestments of Prophecy", text["gear"])
        self.assertIn("-# Insightful Earthsiege Diamond · empty Blue", text["gear"])
        self.assertIn("🟪 **Benediction** `70` · Main hand\n-# Enchanted: +30 Spell Power", text["gear"])
        self.assertNotIn("Neophyte's Shirt", text["gear"])  # cosmetic slots aren't listed
        self.assertEqual(text["flags"], "⚠️ Empty: Ring 1 · No enchant: Head, Chest · 1 empty gem socket")

    def test_no_requirements_set(self):
        with mock.patch.multiple(config, GEARCHECK_MIN_LEVEL=0, GEARCHECK_MIN_ITEM_LEVEL=0):
            text = gear.render_lines(gear.summarise(PROFILE, {"equipped_items": []}, None))
        self.assertNotIn("Requirements", text["overview"])
        self.assertIn("**Talents:** none spent", text["overview"])
        self.assertEqual(text["gear"], "No gear equipped.")


class ProfessionAndRulesetTests(unittest.TestCase):
    PROFESSIONS = {"primaries": [{"profession": {"name": "Tailoring"},
                                  "tiers": [{"skill_points": 63, "max_skill_points": 150}]}],
                   "secondaries": [{"profession": {"name": "Cooking"},
                                    "tiers": [{"skill_points": 16, "max_skill_points": 75}]}]}

    def test_professions(self):
        s = gear.summarise(PROFILE, {"equipped_items": []}, None, professions=self.PROFESSIONS)
        self.assertEqual(s["professions"], [{"name": "Tailoring", "skill": 63, "max": 150, "primary": True},
                                            {"name": "Cooking", "skill": 16, "max": 75, "primary": False}])
        text = gear.render_lines(s)
        self.assertIn("**Professions:** Tailoring 63/150", text["overview"])  # primaries only
        self.assertNotIn("Cooking", text["overview"])

    def test_ruleset_flag(self):
        s = gear.summarise(PROFILE, {"equipped_items": []}, None)
        self.assertEqual((s["ruleset_id"], s["ruleset"]), (2, "Normal"))
        with mock.patch.object(config, "CHARACTER_RULESETS", []):
            self.assertFalse(gear.off_ruleset(s))  # no rulesets set: any is fine
        with mock.patch.object(config, "CHARACTER_RULESETS", [1]):
            self.assertTrue(gear.off_ruleset(s))
            self.assertIn("On the Normal ruleset, not one of the guild's", gear.render_lines(s)["flags"])
        with mock.patch.object(config, "CHARACTER_RULESETS", [1, 2]):
            self.assertFalse(gear.off_ruleset(s))


if __name__ == "__main__":
    unittest.main()
