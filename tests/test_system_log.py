"""system_log.py: lifecycle events, console capture, redaction and limits."""

import logging
import os
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase
from tests.test_audit_api import _fake_bot

import app_version
import db
import system_log
from webapi import auth, server


class SystemLogTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        system_log._flood.update(minute=0, written=0, dropped=0)
        system_log._SECRET_CACHE.update(values=[], loaded=0.0)
        system_log._run.update(shutdown_reason=None, heartbeat=None)


class LifecycleTests(SystemLogTestCase):
    def events(self):
        return [e["event"] for e in reversed(system_log.search()[0])]

    def test_first_start(self):
        system_log.startup()
        self.assertEqual(self.events(), ["bot.started"])
        self.assertEqual(db.get_meta("system_run_state"), "running")

    def test_clean_restart_is_not_a_crash(self):
        system_log.startup()
        system_log.set_shutdown_reason("restarted from the dashboard by Mike")
        system_log.shutdown()
        system_log.startup()
        self.assertEqual(self.events(), ["bot.started", "bot.stopped", "bot.started"])
        stopped = system_log.search({"q": "dashboard"})[0][0]
        self.assertEqual(stopped["message"], "Bot stopped: restarted from the dashboard by Mike.")

    def test_unclean_stop_is_reported_on_next_start(self):
        system_log.startup()
        system_log.startup()  # no shutdown() in between = the process died
        crashed = system_log.search({"category": "lifecycle", "levels": ["ERROR"]})[0]
        self.assertEqual(len(crashed), 1)
        self.assertIn("stopped unexpectedly", crashed[0]["message"])

    def test_version_change_is_recorded(self):
        db.connection()
        db.set_meta("system_version", "abc1234")
        db.set_meta("system_run_state", "stopped")
        with mock.patch.dict(app_version.INFO, {"version": "def5678", "title": "Add things",
                                                "repository": "Owner/Repo", "source": "image"}), \
                mock.patch.object(app_version, "VERSION", "def5678"):
            system_log.startup()
        updated = system_log.search({"q": "Updated"})[0][0]
        self.assertEqual(updated["message"], "Updated from abc1234 to def5678: Add things")
        self.assertEqual(updated["details"]["compare_url"], "https://github.com/Owner/Repo/compare/abc1234...def5678")

    def test_shutdown_reason_not_overwritten_when_asked(self):
        system_log.set_shutdown_reason("first")
        system_log.set_shutdown_reason("second", overwrite=False)
        self.assertEqual(system_log._run["shutdown_reason"], "first")


class ConsoleCaptureTests(SystemLogTestCase):
    def setUp(self):
        super().setUp()
        self.logger = logging.getLogger("tests.system_log.capture")
        self.logger.propagate = False
        self.handler = system_log.ConsoleCapture(level="WARNING")
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        super().tearDown()

    def test_captures_warnings_and_errors_with_traceback(self):
        self.logger.info("not kept")
        self.logger.warning("disk %s", "almost full")
        try:
            1 / 0
        except ZeroDivisionError:
            self.logger.exception("Summary failed")
        entries = system_log.search()[0]
        self.assertEqual([e["message"] for e in entries], ["Summary failed", "disk almost full"])
        self.assertEqual(entries[0]["level"], "ERROR")
        self.assertIn("ZeroDivisionError", entries[0]["details"]["traceback"])
        self.assertEqual(entries[0]["details"]["logger"], "tests.system_log.capture")

    def test_repeats_are_folded(self):
        for _ in range(5):
            self.logger.error("WarcraftLogs timed out")
        entries = system_log.search()[0]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["count"], 5)

    def test_flood_guard(self):
        for i in range(system_log.MAX_NEW_PER_MINUTE + 10):
            self.logger.error("distinct error %d", i)
        self.assertEqual(len(system_log.search(limit=200)[0]), system_log.MAX_NEW_PER_MINUTE)
        self.assertEqual(system_log._flood["dropped"], 10)

    def test_secrets_are_redacted(self):
        with mock.patch.dict(os.environ, {"DISCORD_CLIENT_SECRET": "supersecretvalue123"}):
            self.logger.error("Login failed with supersecretvalue123 and Authorization: Bearer abcdefghijklmnop1234")
            self.logger.error("token=abcdef123456 client_secret: 'qwertyuiop'")
        text = repr(system_log.search()[0])
        for secret in ("supersecretvalue123", "abcdefghijklmnop1234", "abcdef123456", "qwertyuiop"):
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)

    def test_logging_while_storing_does_not_loop(self):
        with mock.patch.object(system_log, "_store", side_effect=RuntimeError("db gone")), \
                self.assertLogs("wow-apply-bot.system_log", level="ERROR"):
            self.logger.error("boom")  # must not raise or recurse
        self.assertEqual(system_log.search()[0], [])

    def test_install_off_and_level(self):
        root = logging.getLogger()
        try:
            self.assertIsNone(system_log.install("OFF"))
            self.assertNotIn(system_log._handler, root.handlers)
            handler = system_log.install("ERROR")
            self.assertIn(handler, root.handlers)
            self.assertEqual(handler.level, logging.ERROR)
        finally:
            system_log.install("OFF")


class RetentionTests(SystemLogTestCase):
    def test_purge(self):
        system_log.record("INFO", "lifecycle", "bot.started", "old")
        db.execute("UPDATE system_log SET last_seen = '2000-01-01T00:00:00Z'")
        system_log.record("INFO", "lifecycle", "bot.started", "new")
        self.assertEqual(system_log.purge(30), 1)
        self.assertEqual(system_log.purge(0), 0)


class SystemLogApiTests(SystemLogTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self._patch = mock.patch.object(auth, "levels_for", fake_levels)
        self._patch.start()
        self.client = TestClient(TestServer(server.create_app(_fake_bot())))
        await self.client.start_server()
        token = auth.create_session({"id": "111", "username": "mike_eg"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        system_log.record("INFO", "lifecycle", "bot.started", "Bot started")
        system_log.record("ERROR", "console", "wow-apply-bot.raid_summary", "Summary failed",
                          {"traceback": "Traceback ..."})

    async def asyncTearDown(self):
        await self.client.close()
        self._patch.stop()

    async def test_moderators_see_bot_events_only(self):
        body = await (await self.client.get("/api/system-log")).json()
        self.assertEqual([e["event"] for e in body["entries"]], ["bot.started"])
        meta = await (await self.client.get("/api/system-log/meta")).json()
        self.assertFalse(meta["console_visible"])

    async def test_admins_see_console_output(self):
        self.levels = {"moderator", "admin"}
        body = await (await self.client.get("/api/system-log")).json()
        self.assertEqual(len(body["entries"]), 2)
        body = await (await self.client.get("/api/system-log?view=errors")).json()
        self.assertEqual([e["message"] for e in body["entries"]], ["Summary failed"])
        meta = await (await self.client.get("/api/system-log/meta")).json()
        self.assertTrue(meta["console_visible"])
        self.assertEqual(meta["errors_24h"], 1)

    async def test_bad_view(self):
        resp = await self.client.get("/api/system-log?view=everything")
        self.assertEqual(resp.status, 400)

    async def test_status_includes_counts_only(self):
        status = server._status(self.client.app)
        self.assertEqual(status["system"]["errors_24h"], 1)
        self.assertNotIn("Summary failed", repr(status))


if __name__ == "__main__":
    unittest.main()
