"""gear.py: character statistics, and which ones the review card shows per role."""

import unittest

import gear

# Trimmed from a real WoW Forever answer (/profile/wow/character/{slug}/statistics, level 28 priest).
STATISTICS = {
    "health": 985, "power": 1729, "power_type": {"name": "Mana", "id": 0},
    "strength": {"base": 24, "effective": 24}, "agility": {"base": 25, "effective": 32},
    "intellect": {"base": 56, "effective": 95}, "stamina": {"base": 32, "effective": 81},
    "spirit": {"base": 65, "effective": 81},
    "melee_crit": {"rating_bonus": 0.0, "value": 1.1712, "rating_normalized": 0},
    "attack_power": 20, "ranged_attack_power": 6, "spell_power": 36, "spell_healing": 45,
    "spell_crit": {"rating_bonus": 0.0, "value": 4.638, "rating_normalized": 0},
    "spell_hit": {"rating_bonus": 0.0, "value": 0.0, "rating_normalized": 0},
    "mana_regen": 86.0, "mana_regen_combat": 5.0,
    "armor": {"base": 10, "effective": 313}, "defense": {"base": 137, "effective": 137},
    "dodge": {"rating_bonus": 0.0, "value": 5.2512, "rating_normalized": 0},
    "shadow_resistance": {"base": 5, "effective": 5},
    "versatility": 0.0,  # a modern-game field we don't show
    "character": {"name": "Eggmike"},
}


class StatTests(unittest.TestCase):
    def test_parse_reads_effective_values_and_percentages(self):
        stats = gear.parse_stats(STATISTICS)
        self.assertEqual(stats["intellect"], 95)
        self.assertEqual(stats["armor"], 313)
        self.assertAlmostEqual(stats["spell_crit"], 4.64)
        self.assertNotIn("versatility", stats)
        self.assertNotIn("parry", stats)  # not in the answer: left out, not shown as 0
        self.assertEqual(gear.parse_stats(None), {})

    def test_highlights_follow_the_role(self):
        stats = gear.parse_stats(STATISTICS)
        healer = [h["label"] for h in gear.stat_highlights(stats, "Healer")]
        self.assertEqual(healer[:3], ["Mana / power", "Intellect", "Spirit"])
        self.assertIn("Bonus healing", healer)
        tank = {h["key"]: h["text"] for h in gear.stat_highlights(stats, "Tank")}
        self.assertEqual(tank["dodge"], "5.25%")
        self.assertEqual(tank["armor"], "313")

    def test_dps_is_caster_melee_or_ranged_by_the_sheet(self):
        self.assertEqual(gear.stat_profile({"spell_power": 300, "attack_power": 100}, "DPS"), "caster")
        self.assertEqual(gear.stat_profile({"attack_power": 900, "ranged_attack_power": 1200}, "DPS"), "ranged")
        self.assertEqual(gear.stat_profile({"attack_power": 1500, "ranged_attack_power": 100}, None), "melee")

    def test_groups_skip_empty_ones(self):
        groups = {g["title"]: [s["key"] for s in g["stats"]] for g in gear.stat_groups(gear.parse_stats(STATISTICS))}
        self.assertIn("spell_healing", groups["Spell"])
        self.assertEqual(groups["Resistances"], ["shadow_resistance"])
        self.assertNotIn("Ranged", [t for t in groups if not groups[t]])

    def test_card_shows_role_stats(self):
        summary = {"stats": gear.parse_stats(STATISTICS), "items": [], "talents": [], "professions": []}
        overview = gear.render_lines(summary, role="Healer")["overview"]
        self.assertIn("**Stats:** Mana / power 1,729 · Intellect 95", overview)
        self.assertNotIn("**Stats:**", gear.render_lines({"items": [], "talents": []})["overview"])


if __name__ == "__main__":
    unittest.main()
