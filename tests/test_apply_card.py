"""application_card.py (through cogs/apply.py): the review card in Discord."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from tests.test_gear import EQUIPMENT, PROFILE, SPECS
from tests.test_gear_stats import STATISTICS

import application_card
import config
import gear
from cogs import apply

PROFESSIONS = {"primaries": [{"profession": {"name": "Enchanting", "id": 333},
                              "tiers": [{"skill_points": 200, "max_skill_points": 225}]}]}


def texts(view) -> str:
    return "\n".join(i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay))


def buttons(view) -> dict:
    return {(c.custom_id or c.label): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def summary(**extra):
    s = gear.summarise(PROFILE, EQUIPMENT, SPECS, professions=PROFESSIONS, statistics=STATISTICS)
    return {**s, "avatar_url": "https://render.worldofwarcraft.com/forever-eu/character/62/1-avatar.jpg", **extra}


class ReviewCardTests(unittest.TestCase):
    def setUp(self):
        self.cog = apply.ApplyCog.__new__(apply.ApplyCog)
        self.cog.bot = SimpleNamespace(store=SimpleNamespace(items=lambda: []))
        self._config = mock.patch.multiple(config, FEATURE_WEB_DASHBOARD_ENABLED=True, REVIEW_CHANNEL_ID=0)
        self._config.start()

    def tearDown(self):
        self._config.stop()

    def card(self, message_id=5, **extra):
        record = {"applicant_id": 1, "character_name": "Eggmike Thewhite", "character_slug": "eggmike-thewhite",
                  "status": "pending", "class_name": "Priest", "class_color": 0xFFFFFF, "spec": "Holy",
                  "role": "Healer", "other_specs": ["Discipline"], "submitted_at": 1793870000, **extra}
        return self.cog._render_application_view(record, message_id=message_id)

    def test_pending_card(self):
        view = self.card(gear=summary(), gear_checked_at=1793870276, note="Raided every tier.\nCan heal too.",
                         icons={"item:%d" % EQUIPMENT["equipped_items"][0]["item"]["id"]: "<:benediction:1>",
                                "profession:333": "<:enchanting:2>"})
        text = texts(view)
        self.assertIn("## Eggmike Thewhite", text)
        self.assertIn("Level 60 Undead Priest · ‹Encore› · Normal", text)
        self.assertIn("🟡 **Waiting for review** · applied <t:1793870000:R>", text)
        self.assertIn("**Main spec** Holy · Healer", text)
        self.assertIn("**Offspec** Discipline", text)
        self.assertIn("**Item level** `60` (62 with bags) · **Talents** Holy 30 / Discipline 21", text)
        self.assertIn("### ⚠️ Worth a look\n- Empty: Neck", text)
        self.assertIn("`1,729` Mana / power · `95` Intellect", text)  # healer stats
        self.assertIn("**Professions** <:enchanting:2> Enchanting 200/225", text)
        self.assertIn("<:benediction:1> **[Benediction](https://www.wowhead.com/forever/item=", text)
        self.assertIn("-# ✨ +30 Spell Power", text)
        self.assertIn("checked <t:1793870276:R>", text)
        self.assertIn("### Note\n> Raided every tier.\n> Can heal too.", text)
        # The character's portrait sits next to the header.
        [section] = [c for c in view.walk_children() if isinstance(c, discord.ui.Section)]
        self.assertTrue(section.accessory.media.url.endswith("-avatar.jpg"))
        [container] = [c for c in view.walk_children() if isinstance(c, discord.ui.Container)]
        self.assertEqual(container.accent_color.value, 0xFFFFFF)  # class colour while pending
        # Only what fits a pending application, plus a link to the dashboard.
        found = buttons(view)
        self.assertEqual(set(found), {"wow_apply_approve", "wow_apply_deny", "wow_apply_refresh_gear",
                                      "Open on dashboard"})
        self.assertTrue(found["Open on dashboard"].url.endswith("/applications/5"))
        self.assertLessEqual(len(text), 4000)

    def test_decided_cards(self):
        approved = self.card(status="approved", last_decision="approved", gear=summary(), decision={
            "status": "approved", "by_id": 2, "by_name": "officer", "via": "dashboard", "at": 1793880000,
            "roles_assigned": ["Fresh", "Healer"], "roles_kept": [], "nickname": "Name changed: a → b",
            "roster": "Guild roster: character linked to the applicant", "reason": None})
        text = texts(approved)
        self.assertIn("✅ **Approved** by officer · <t:1793880000:R>", text)
        self.assertIn("✅ **Approved** by **officer on the dashboard** · <t:1793880000:f>", text)
        self.assertIn("-# Roles given: Fresh, Healer", text)
        self.assertEqual(set(buttons(approved)), {"wow_apply_reset", "wow_apply_refresh_gear", "Open on dashboard"})
        [container] = [c for c in approved.walk_children() if isinstance(c, discord.ui.Container)]
        self.assertEqual(container.accent_color.value, application_card.APPROVED_COLOR)

        denied = self.card(status="denied", last_decision="denied", decision={
            "status": "denied", "by_id": 2, "by_name": "officer", "via": "discord", "at": 1793880000,
            "roles_assigned": [], "roles_kept": [], "nickname": None, "roster": None, "reason": "Too low"})
        self.assertIn("-# Reason (reviewers only): Too low", texts(denied))

    def test_persistent_template_answers_every_button(self):
        view = self.cog._render_application_view({"applicant_id": 0, "character_name": "template",
                                                  "character_slug": "template", "status": "pending"},
                                                 all_buttons=True)
        self.assertEqual(set(buttons(view)), {"wow_apply_approve", "wow_apply_deny", "wow_apply_reset",
                                              "wow_apply_refresh_gear"})
        self.assertTrue(view.is_persistent())

    def test_long_cards_stay_within_discords_limit(self):
        items = [{"slot": "HEAD", "slot_name": "Head", "item_id": 100000 + i, "name": f"Some Long Item Name {i}",
                  "quality": "EPIC", "item_level": 60, "stats": [], "enchants": ["Enchanted: +10 Stamina"],
                  "sockets": [{"type": "Red", "gem": "Bold Ruby", "text": None}], "set": None} for i in range(40)]
        text = texts(self.card(gear=summary(items=items), note="n" * 1500))
        self.assertIn("the rest is on the dashboard's Applications page", text)
        self.assertLessEqual(len(text), 4000)
        short = texts(self.card(gear=summary(items=items[:17])))
        self.assertNotIn("the rest is on the dashboard", short)  # a full set fits a normal card

    def test_older_applications_still_render(self):
        view = self.card(gear=None, spec="Holy", class_block="**Class:** Priest\n**Level:** 70",
                         armory_block="**Equipped Gear** (old)", profile_url="https://fresh.warcraftlogs.com/x",
                         decision_footer="✅ Approved by mike • 2026-01-01", status="approved",
                         last_decision="approved")
        text = texts(view)
        self.assertIn("## [Eggmike Thewhite](https://fresh.warcraftlogs.com/x)", text)
        self.assertIn("**Level:** 70", text)
        self.assertIn("**Equipped Gear** (old)", text)
        self.assertIn("✅ Approved by mike • 2026-01-01", text)
        self.assertEqual([c for c in view.walk_children() if isinstance(c, discord.ui.Section)], [])  # no portrait

    def test_no_dashboard_link_when_it_is_off(self):
        with mock.patch.object(config, "FEATURE_WEB_DASHBOARD_ENABLED", False):
            self.assertNotIn("Open on dashboard", buttons(self.card()))


class LookupViewTests(unittest.TestCase):
    """/lookup draws a character with the application card's parts."""

    def test_same_parts_without_the_application(self):
        with mock.patch.object(config, "GEARCHECK_MIN_ITEM_LEVEL", 0):
            view = application_card.lookup_view(
                summary(), icons_by_key={"profession:333": "<:enchanting:2>"},
                extra_facts=["**Guild roster** <@111> · main"], dashboard_url="http://localhost:8081/lookup?name=x")
        text = texts(view)
        self.assertIn("## Eggmike Thewhite\nLevel 60 Undead Priest · ‹Encore› · Normal", text)
        self.assertNotIn("Waiting for review", text)  # no application status
        self.assertNotIn("Applicant", text)
        self.assertIn("**Guild roster** <@111> · main", text)
        self.assertIn("**Item level** `60` (62 with bags)", text)
        self.assertIn("### ⚠️ Worth a look", text)
        self.assertIn("**Professions** <:enchanting:2> Enchanting 200/225", text)
        self.assertIn("### Gear", text)
        self.assertNotIn("### Note", text)
        [section] = [c for c in view.walk_children() if isinstance(c, discord.ui.Section)]
        self.assertTrue(section.accessory.media.url.endswith("-avatar.jpg"))
        self.assertEqual(set(buttons(view)), {"Open in Character lookup"})  # a link only, nothing to decide
        [container] = [c for c in view.walk_children() if isinstance(c, discord.ui.Container)]
        self.assertEqual(container.accent_color.value, config.CLASS_COLORS["Priest"])

    def test_long_gear_points_to_the_lookup_page(self):
        items = [{"slot": "HEAD", "slot_name": "Head", "item_id": 100000 + i, "name": f"Some Long Item Name {i}",
                  "quality": "EPIC", "item_level": 60, "stats": [], "enchants": ["Enchanted: +10 Stamina"],
                  "sockets": [], "set": None} for i in range(80)]
        text = texts(application_card.lookup_view(summary(items=items)))
        self.assertIn("the rest is on the dashboard's Character lookup", text)
        self.assertLessEqual(len(text), 4000)


class PortraitTests(unittest.TestCase):
    def test_portraits_from_character_media(self):
        media = {"assets": [{"value": "https://r/x/1-main-raw.png"}, {"value": "https://r/x/1-avatar.jpg"},
                            {"value": "https://r/x/1-avatar-sd.jpg"}]}
        self.assertEqual(gear.portraits(media), {"avatar_url": "https://r/x/1-avatar.jpg",
                                                 "render_url": "https://r/x/1-main-raw.png"})
        self.assertEqual(gear.portraits(None), {"avatar_url": None, "render_url": None})


if __name__ == "__main__":
    unittest.main()
