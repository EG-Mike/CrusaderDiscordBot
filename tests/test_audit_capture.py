"""cogs/audit_log.py: turning Discord interaction payloads into audit events."""

import unittest
from types import SimpleNamespace

from tests.helpers import ROOT  # noqa: F401 (sets sys.path)

from cogs import audit_log


class DescribeTests(unittest.TestCase):
    def test_slash_command_with_subcommand_and_resolved_user(self):
        data = {
            "type": 1,
            "name": "checkattendance",
            "options": [{"type": 1, "name": "link", "options": [
                {"type": 6, "name": "member", "value": "55"},
                {"type": 3, "name": "character", "value": "Thrall"},
            ]}],
            "resolved": {"users": {"55": {"username": "jaina"}}},
        }
        event = audit_log.describe_app_command(data)
        self.assertEqual(event["kind"], "command")
        self.assertEqual(event["action"], "/checkattendance link")
        self.assertEqual(event["details"]["options"], {"member": "@jaina(55)", "character": "Thrall"})
        self.assertEqual(event["text"], "COMMAND /checkattendance link member=@jaina(55) character=Thrall")

    def test_long_option_values_are_truncated(self):
        data = {"type": 1, "name": "poll", "options": [{"type": 3, "name": "question", "value": "x" * 500}]}
        value = audit_log.describe_app_command(data)["details"]["options"]["question"]
        self.assertEqual(len(value), audit_log.MAX_VALUE_LEN)

    def test_context_menu(self):
        event = audit_log.describe_app_command({"type": 2, "name": "Gear check", "target_id": "77"})
        self.assertEqual(event["kind"], "context_menu")
        self.assertEqual(event["target"], "77")
        self.assertEqual(event["summary"], 'Used "Gear check" on a user')

    def test_button_uses_label_from_message(self):
        button = SimpleNamespace(custom_id="wow_apply_approve", label="Approve", emoji=None)
        message = SimpleNamespace(id=1234, components=[SimpleNamespace(children=[button])])
        event = audit_log.describe_component({"custom_id": "wow_apply_approve", "component_type": 2}, message)
        self.assertEqual(event["kind"], "button")
        self.assertEqual(event["action"], "Approve")
        self.assertEqual(event["summary"], 'Clicked "Approve"')
        self.assertEqual(event["target"], "1234")
        self.assertEqual(event["text"], 'BUTTON wow_apply_approve "Approve" msg=1234')

    def test_select_values(self):
        event = audit_log.describe_component({"custom_id": "tier", "component_type": 3, "values": ["SSC", "TK"]})
        self.assertEqual(event["kind"], "select")
        self.assertEqual(event["details"]["values"], ["SSC", "TK"])
        self.assertIsNone(event["target"])

    def test_modal_never_records_what_was_typed(self):
        secret = "my private applicant note"
        data = {"custom_id": "apply_modal", "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": "note", "value": secret}]},
            {"type": 18, "component": {"type": 3, "custom_id": "role", "values": ["Healer"]}},
        ]}
        event = audit_log.describe_modal(data)
        self.assertEqual(event["details"]["fields"], [f"note({len(secret)} chars)", "role(1 picked)"])
        self.assertNotIn(secret, repr(event))
        self.assertNotIn("Healer", repr(event))


if __name__ == "__main__":
    unittest.main()
