"""cogs/apply.py: the review card with the Blizzard gear summary."""

import unittest
from types import SimpleNamespace

import discord

from tests.test_gear import EQUIPMENT, PROFILE, SPECS

import gear
from cogs import apply


def texts(view) -> str:
    out = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            out.append(item.content)
    return "\n".join(out)


class ReviewCardTests(unittest.TestCase):
    def setUp(self):
        self.cog = apply.ApplyCog.__new__(apply.ApplyCog)
        self.cog.bot = SimpleNamespace(store=SimpleNamespace(items=lambda: []))

    def card(self, **extra):
        record = {"applicant_id": 1, "character_name": "Eggmike Thewhite", "status": "pending", **extra}
        return self.cog._render_application_view(record, message_id=5)

    def test_gear_section_and_refresh_button(self):
        summary = gear.summarise(PROFILE, EQUIPMENT, SPECS)
        view = self.card(character_slug="eggmike-thewhite", gear=summary, gear_text=gear.render_lines(summary),
                         gear_checked_at=1793870276)
        text = texts(view)
        self.assertIn("### Gear", text)
        self.assertIn("<t:1793870276:R>", text)
        self.assertIn("**Benediction**", text)
        buttons = {c.custom_id: c for c in view.walk_children() if isinstance(c, discord.ui.Button)}
        self.assertFalse(buttons["wow_apply_refresh_gear"].disabled)
        self.assertLessEqual(len(text), 4000)

    def test_old_applications_still_render(self):
        view = self.card(armory_block="**Equipped Gear** (old)", profile_url="https://fresh.warcraftlogs.com/x")
        text = texts(view)
        self.assertIn("**Equipped Gear** (old)", text)
        self.assertIn("[Profile](https://fresh.warcraftlogs.com/x)", text)
        buttons = {c.custom_id: c for c in view.walk_children() if isinstance(c, discord.ui.Button)}
        self.assertTrue(buttons["wow_apply_refresh_gear"].disabled)  # no character to refresh

    def test_long_gear_lists_are_clipped(self):
        long_gear = "\n".join(f"<:icon_{i}:1234567890123456789> **Some Very Long Item Name {i}** `100` · Slot"
                              for i in range(60))
        text = texts(self.card(character_slug="x", gear_text={"overview": "o", "gear": long_gear, "flags": ""}))
        self.assertIn("more items not shown", text)
        self.assertLessEqual(len(text), 4000)


if __name__ == "__main__":
    unittest.main()
