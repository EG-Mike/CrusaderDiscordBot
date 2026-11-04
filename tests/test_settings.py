"""webapi/settings.py and webapi/settings_catalog.py: what the Settings page shows and accepts."""

import unittest
from types import SimpleNamespace

from tests.helpers import DatabaseTestCase

import config.deployment as deployment
from webapi import settings, settings_catalog as catalog


class CatalogTests(unittest.TestCase):
    def test_every_setting_is_catalogued_or_hidden(self):
        keys = {k for k in vars(deployment) if k.isupper()}
        missing = keys - set(catalog.CATALOG) - set(catalog.HIDDEN)
        self.assertEqual(missing, set(), "Add these to webapi/settings_catalog.py (CATALOG, or HIDDEN with a reason)")
        self.assertEqual((set(catalog.CATALOG) | set(catalog.HIDDEN)) - keys, set(), "Catalogued but not in deployment.py")
        self.assertEqual(set(catalog.CATALOG) & set(catalog.HIDDEN), set())

    def test_groups_and_sections_exist(self):
        sections = {sid for sid, _ in catalog.SECTIONS}
        groups = {gid for gid, section, _, _ in catalog.GROUPS}
        self.assertTrue(all(section in sections for _, section, _, _ in catalog.GROUPS))
        for key, entry in catalog.CATALOG.items():
            self.assertIn(entry.group, groups, key)
            self.assertNotRegex(entry.help + entry.label, r"\b[A-Z]{3,}_[A-Z_]{3,}\b", f"{key}: no setting names "
                                                                                        f"in user-facing text")

    def test_choices_include_the_default(self):
        for key, entry in catalog.CATALOG.items():
            if entry.choices:
                self.assertIn(getattr(deployment, key), [v for v, _ in entry.choices], key)


class SettingsTests(DatabaseTestCase):
    def test_hidden_settings_are_not_listed_or_editable(self):
        listed = {s["key"] for s in settings.list_settings()["settings"]}
        self.assertNotIn("CLASS_ICON_URLS", listed)
        with self.assertRaisesRegex(settings.SettingError, "Unknown"):
            settings.validate("CLASS_ICON_URLS", {})

    def test_describe(self):
        s = settings.describe("SYSTEM_LOG_RETENTION_DAYS")
        self.assertEqual((s["group"], s["unit"], s["advanced"]), ("logging", "days", False))
        level = settings.describe("SYSTEM_LOG_LEVEL")
        self.assertEqual(level["kind"], "enum")
        self.assertIn({"value": "WARNING", "label": "Warnings and errors"}, level["options"])
        self.assertTrue(settings.describe("GUILD_DISPLAY_NAME")["live"])
        data = settings.list_settings()
        self.assertEqual([s["id"] for s in data["sections"]], ["general", "features", "integrations", "system"])
        self.assertIn("setup", [g["id"] for g in data["groups"]])  # custom panel, no settings of its own

    def test_validation(self):
        self.assertEqual(settings.validate("SERVER_REGION", "eu"), "eu")
        self.assertEqual(settings.validate("EMOJI_WOWHEAD_DATA_ENV", 1), 1)
        for key, value, message in [("SERVER_REGION", "mars", "pick one"), ("MAX_SCREENSHOTS", 12, "between 1 and 9"),
                                    ("LEVELING_REFRESH_MINUTES", 0, "between"),
                                    ("RAID_LOG_AUTO_SUMMARIZE_TIME", "25:00", "HH:MM")]:
            with self.assertRaisesRegex(settings.SettingError, message, msg=key):
                settings.validate(key, value)

    def test_live_setting_resets_to_its_real_default(self):
        import config
        original = config.GUILD_DISPLAY_NAME
        try:
            settings.save("GUILD_DISPLAY_NAME", "Crusader", "Mike (1)")
            self.assertEqual(config.GUILD_DISPLAY_NAME, "Crusader")  # applied at once
            self.assertEqual(settings.describe("GUILD_DISPLAY_NAME")["default"], original)
            settings.reset("GUILD_DISPLAY_NAME", "Mike (1)")
            self.assertEqual(config.GUILD_DISPLAY_NAME, original)
            self.assertFalse(settings.describe("GUILD_DISPLAY_NAME")["overridden"])
        finally:
            config.GUILD_DISPLAY_NAME = original
            settings._STARTUP_VALUES.pop("GUILD_DISPLAY_NAME", None)

    def test_role_widgets_check_the_roles(self):
        guild = SimpleNamespace(name="Test", get_role=lambda rid: object() if rid == 111 else None)
        typed = settings.validate("FRESH_EXEMPT_ROLE_IDS", {"111": "Regular"}, guild)
        self.assertEqual(typed, {111: "Regular"})
        with self.assertRaisesRegex(settings.SettingError, "No role with ID 222"):
            settings.validate("FRESH_EXEMPT_ROLE_IDS", {"222": "Gone"}, guild)
        with self.assertRaisesRegex(settings.SettingError, "No role"):
            settings.validate("AUTO_ROLE_IDS", {"Tank": "222"}, guild)


if __name__ == "__main__":
    unittest.main()
