"""audit.py: storing, searching, retention and the old text-file import."""

import logging
import os
from datetime import datetime, timedelta, timezone

from tests.helpers import DatabaseTestCase

import audit
import db


def _discord(**overrides):
    event = dict(source="discord", kind="command", action="/apply", summary="/apply character=Thrall",
                 actor_id=111, actor_name="Mike", channel_id=222, channel_name="gear-check")
    event.update(overrides)
    return audit.record(**event)


class RecordAndSearchTests(DatabaseTestCase):
    def test_record_returns_event_with_string_ids(self):
        event = _discord(details={"options": {"character": "Thrall"}})
        self.assertIsNotNone(event)
        self.assertEqual(event["actor_id"], "111")
        self.assertEqual(event["channel_id"], "222")
        self.assertEqual(event["details"], {"options": {"character": "Thrall"}})

    def test_unknown_kind_is_stored_as_other(self):
        self.assertEqual(_discord(kind="nonsense")["kind"], "other")

    def test_bad_source_is_logged_not_raised(self):
        with self.assertLogs("wow-apply-bot.audit_store", level="ERROR"):
            self.assertIsNone(_discord(source="nowhere"))

    def test_oversized_details_are_dropped(self):
        event = _discord(details={"blob": "x" * (audit.MAX_DETAILS_CHARS + 10)})
        self.assertEqual(event["details"], {"truncated": True})

    def test_search_newest_first_with_paging(self):
        for i in range(5):
            _discord(summary=f"event {i}")
        page, more = audit.search(limit=3)
        self.assertEqual([e["summary"] for e in page], ["event 4", "event 3", "event 2"])
        self.assertTrue(more)
        page2, more2 = audit.search(before_id=page[-1]["id"], limit=3)
        self.assertEqual([e["summary"] for e in page2], ["event 1", "event 0"])
        self.assertFalse(more2)

    def test_filters(self):
        _discord()
        _discord(actor_id=999, actor_name="Other", kind="button", action="Approve", summary='Clicked "Approve"')
        audit.record(source="dashboard", kind="setting", action="settings.update", summary="Changed setting X",
                     actor_id=111, actor_name="Mike", target="X")
        self.assertEqual(len(audit.search({"source": "dashboard"})[0]), 1)
        self.assertEqual(len(audit.search({"kind": "button"})[0]), 1)
        self.assertEqual(len(audit.search({"actor_id": "111"})[0]), 2)
        self.assertEqual(len(audit.search({"channel_id": "222"})[0]), 2)
        self.assertEqual(len(audit.search({"q": "approve"})[0]), 1)  # case-insensitive
        self.assertEqual(len(audit.search({"q": "other"})[0]), 1)    # matches the member name

    def test_search_escapes_like_wildcards(self):
        _discord(summary="100% done")
        _discord(summary="nothing here")
        self.assertEqual(len(audit.search({"q": "%"})[0]), 1)
        self.assertEqual(len(audit.search({"q": "_"})[0]), 0)

    def test_time_range(self):
        _discord(created_at="2026-01-01T10:00:00Z")
        _discord(created_at="2026-02-01T10:00:00Z")
        since = audit.parse_time("2026-01-15")
        self.assertEqual(len(audit.search({"since": since})[0]), 1)
        until = audit.parse_time("2026-01-15T00:00:00+01:00")
        self.assertEqual(audit.search({"until": until})[0][0]["created_at"], "2026-01-01T10:00:00Z")

    def test_parse_time_rejects_garbage(self):
        with self.assertRaises(ValueError):
            audit.parse_time("yesterday")

    def test_listeners_get_new_events(self):
        seen = []
        unsubscribe = audit.subscribe(seen.append)
        try:
            _discord()
        finally:
            unsubscribe()
        _discord()
        self.assertEqual(len(seen), 1)

    def test_text_line_is_written_in_the_original_format(self):
        with self.assertLogs("wow-apply-bot.audit", level="INFO") as captured:
            audit.record(source="dashboard", kind="setting", action="settings.update", summary="Changed",
                         actor_id=5, actor_name="Mike", target="MOD_ROLE_ID", created_at="2026-11-03T19:42:10Z")
        self.assertEqual(captured.records[-1].getMessage(),
                         "2026-11-03 19:42:10Z\tMike (5)\tweb dashboard\tWEB settings.update MOD_ROLE_ID")

    def test_facets_and_stats(self):
        _discord(actor_name="Old name")
        _discord(actor_name="New name")
        _discord(actor_id=2, actor_name="Two", channel_id=None, channel_name="DM")
        facets = audit.facets()
        self.assertEqual(facets["actors"][0], {"id": "111", "name": "New name", "count": 2})
        self.assertEqual([c["id"] for c in facets["channels"]], ["222"])
        stats = audit.stats()
        self.assertEqual(stats["events_24h"], 3)
        self.assertEqual(stats["members_7d"], 2)
        self.assertEqual(stats["total"], 3)


class RetentionTests(DatabaseTestCase):
    def test_purge_removes_only_old_events(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(audit.TIME_FORMAT)
        _discord(created_at=old)
        _discord()
        self.assertEqual(audit.purge(30), 1)
        self.assertEqual(len(audit.search()[0]), 1)

    def test_zero_keeps_everything(self):
        _discord(created_at="2000-01-01T00:00:00Z")
        self.assertEqual(audit.purge(0), 0)


SAMPLE_LINES = [
    "2026-11-03 19:42:10Z\tMike [mike_eg] (123456789)\t#gear-check(987654321)\t"
    "BUTTON wow_apply_approve \"Approve\" msg=1122334455",
    "2026-11-03 19:43:00Z\tmike_eg (123456789)\t#raid(42)\tCOMMAND /poll create question=Raid night? duration=1d",
    "2026-11-03 19:44:00Z\tmike_eg (123456789)\tDM\tMODAL apply_modal fields=note(12 chars)",
    "2026-11-03 19:45:00Z\tmike_eg (123456789)\tchannel(42)\tSELECT pick_tier values=SSC,TK msg=5",
    "2026-11-03 19:46:00Z\tmike_eg (123456789)\t#raid(42)\tCONTEXT_MENU \"Gear check\" on user 77",
    "2026-11-03 19:47:00Z\tMike (123456789)\tweb dashboard\tWEB settings.update MOD_ROLE_ID",
    "2026-11-03 19:48:00Z\tmike_eg (123456789)\t#raid(42)\tSOMETHING new",
]


class TextImportTests(DatabaseTestCase):
    def test_parse_each_line_type(self):
        parsed = [audit.parse_text_line(line) for line in SAMPLE_LINES]
        button, command, modal, select, context, web, other = parsed

        self.assertEqual(button["kind"], "button")
        self.assertEqual(button["action"], "Approve")
        self.assertEqual(button["actor_name"], "Mike")
        self.assertEqual(button["details"]["username"], "mike_eg")
        self.assertEqual(button["details"]["message_id"], "1122334455")
        self.assertEqual(button["channel_name"], "gear-check")
        self.assertEqual(button["created_at"], "2026-11-03T19:42:10Z")

        self.assertEqual(command["action"], "/poll create")
        self.assertEqual(command["details"]["options_text"], "question=Raid night? duration=1d")

        self.assertEqual(modal["channel_name"], "DM")
        self.assertEqual(modal["details"]["fields"], ["note(12 chars)"])

        self.assertEqual(select["channel_id"], "42")
        self.assertEqual(select["details"]["values"], ["SSC", "TK"])
        self.assertIn("SSC, TK", select["summary"])

        self.assertEqual(context["kind"], "context_menu")
        self.assertEqual(context["target"], "77")

        self.assertEqual(web["source"], "dashboard")
        self.assertEqual(web["kind"], "setting")
        self.assertEqual(web["target"], "MOD_ROLE_ID")
        self.assertEqual(web["summary"], "Changed setting MOD_ROLE_ID")

        self.assertEqual(other["kind"], "other")
        self.assertEqual(other["summary"], "SOMETHING new")

    def test_non_audit_lines_are_skipped(self):
        self.assertIsNone(audit.parse_text_line("random text"))
        self.assertIsNone(audit.parse_text_line("not a date\ta\tb\tc"))

    def test_import_runs_once_oldest_file_first(self):
        path = os.path.join(self._tmp.name, "audit.log")
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        with open(path + ".1", "w", encoding="utf-8") as f:
            f.write(f"{recent}\tA (1)\tDM\tMODAL first\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{recent}\tB (2)\tDM\tMODAL second\n")
            f.write("1999-01-01 00:00:00Z\tC (3)\tDM\tMODAL too_old\n")
        self.assertEqual(audit.import_text_files(path, 5), 2)
        events, _ = audit.search()
        self.assertEqual([e["action"] for e in events], ["second", "first"])
        self.assertEqual(audit.import_text_files(path, 5), 0)  # already imported
        self.assertTrue(db.get_meta("audit_text_imported_at"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    import unittest

    unittest.main()
