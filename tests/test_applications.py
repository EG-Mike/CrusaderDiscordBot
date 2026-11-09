"""Applications: the decision rules (cogs/apply.py), shared helpers
(applications.py) and the dashboard API (webapi/applications_api.py)."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import DatabaseTestCase

import applications
import config
from applications import ApplicationError, Reviewer
from cogs import apply
from webapi import auth, server

APPROVER_ROLE, FRESH_ROLE, REGULAR_ROLE, DPS_ROLE, PRIEST_ROLE = 900, 901, 902, 903, 904
REVIEW_CHANNEL = 50
MESSAGE_ID = 1300000000000000000  # a snowflake from 2024


class FakeStore:
    """Enough of storage.ApplicationStore for the rules."""

    def __init__(self, records=None):
        self.data = {str(k): dict(v) for k, v in (records or {}).items()}

    def items(self):
        return list(self.data.items())

    def get(self, key):
        return self.data.get(str(key))

    def update(self, key, **fields):
        self.data[str(key)].update(fields)

    def set(self, key, **fields):
        self.data[str(key)] = dict(fields)

    def add(self, key, applicant_id, character_name, **extra):
        self.set(key, applicant_id=applicant_id, character_name=character_name, **extra)

    def delete(self, key):
        self.data.pop(str(key), None)


def role(role_id, name="role"):
    return SimpleNamespace(id=role_id, name=name)


def record(**extra):
    return {"applicant_id": 111, "character_name": "Eggmike Thewhite", "character_slug": "eggmike-thewhite",
            "status": "pending", "class_name": "Priest", "role": "DPS", "spec": "Shadow Magic", "other_specs": [],
            "last_decision": None, "channel_id": REVIEW_CHANNEL, **extra}


class Setup:
    """A fake server with one applicant and one review card."""

    def make(self, *, applicant_roles=(), records=None):
        self.member = mock.MagicMock()
        self.member.id = 111
        self.member.display_name = "Mike"
        self.member.mention = "<@111>"
        self.member.roles = [role(r) for r in applicant_roles]
        self.member.add_roles = mock.AsyncMock(side_effect=lambda r, reason=None: self.member.roles.append(r))
        self.member.remove_roles = mock.AsyncMock()
        self.member.edit = mock.AsyncMock()
        self.member.send = mock.AsyncMock()
        roles = {i: role(i) for i in (FRESH_ROLE, REGULAR_ROLE, DPS_ROLE, PRIEST_ROLE)}
        self.guild = SimpleNamespace(id=1, emojis=[], get_role=roles.get, get_member=lambda i: self.member if i == 111 else None,
                                     fetch_member=mock.AsyncMock(side_effect=Exception("unused")))
        self.message = mock.MagicMock()
        self.message.edit = mock.AsyncMock()
        self.channel = SimpleNamespace(id=REVIEW_CHANNEL, guild=self.guild, send=mock.AsyncMock(),
                                       fetch_message=mock.AsyncMock(return_value=self.message))
        self.store = FakeStore(records if records is not None else {MESSAGE_ID: record()})
        self.bot = SimpleNamespace(store=self.store, guilds=[self.guild], blizzard=None,
                                   get_channel=lambda cid: self.channel if cid == REVIEW_CHANNEL else None,
                                   get_cog=lambda name: None)
        self.cog = apply.ApplyCog(self.bot)
        self.reviewer = Reviewer(id=222, name="officer", via="dashboard")


CONFIG = dict(REVIEW_CHANNEL_ID=REVIEW_CHANNEL, FRESH_ROLE_ID=FRESH_ROLE, APPROVER_ROLE_ID=APPROVER_ROLE,
              FRESH_EXEMPT_ROLE_IDS={REGULAR_ROLE: "Regular"}, AUTO_ROLE_IDS={"DPS": DPS_ROLE, "Priest": PRIEST_ROLE})


class DecisionTests(Setup, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._config = mock.patch.multiple(config, **CONFIG)
        self._config.start()
        self._messages = mock.patch.object(apply.bot_messages, "text", lambda key, **kw: f"{key}:{kw}")
        self._messages.start()

    def tearDown(self):
        self._messages.stop()
        self._config.stop()

    async def test_approve_gives_roles_nickname_dm_and_updates_card(self):
        self.make()
        changed = []
        applications.on_change("test", changed.append)
        try:
            result = await self.cog.approve(MESSAGE_ID, self.reviewer)
        finally:
            applications._listeners.pop("test", None)
        self.assertEqual(result["status"], "approved")
        given = {r.id for r in self.member.roles}
        self.assertEqual(given, {FRESH_ROLE, DPS_ROLE, PRIEST_ROLE})
        self.member.edit.assert_awaited_once()
        self.assertEqual(self.member.edit.await_args.kwargs["nick"], "Eggmike Thewhite")
        self.member.send.assert_awaited_once()
        self.assertIn("approval_dm", self.member.send.await_args.args[0])
        self.message.edit.assert_awaited_once()  # the card in Discord follows
        self.assertEqual(changed, [str(MESSAGE_ID)])
        decision = result["decision"]
        self.assertEqual((decision["by_name"], decision["via"]), ("officer", "dashboard"))
        self.assertIn("Approved by officer (dashboard)", result["decision_footer"])

    async def test_exempt_member_keeps_name_and_still_gets_class_roles(self):
        self.make(applicant_roles=[REGULAR_ROLE])
        result = await self.cog.approve(MESSAGE_ID, self.reviewer)
        self.assertNotIn(FRESH_ROLE, {r.id for r in self.member.roles})
        self.assertIn(PRIEST_ROLE, {r.id for r in self.member.roles})
        self.member.edit.assert_not_awaited()
        self.assertIn("Regular", result["decision"]["roles_kept"])

    async def test_decided_applications_need_a_reset_first(self):
        self.make(records={MESSAGE_ID: record(status="approved", last_decision="approved")})
        with self.assertRaises(ApplicationError):
            await self.cog.approve(MESSAGE_ID, self.reviewer)
        with self.assertRaises(ApplicationError):
            await self.cog.deny(MESSAGE_ID, self.reviewer)
        reset = await self.cog.reset(MESSAGE_ID, self.reviewer)
        self.assertEqual((reset["status"], reset["last_decision"], reset["decision"]), ("pending", "approved", None))
        with self.assertRaises(ApplicationError):
            await self.cog.reset(MESSAGE_ID, self.reviewer)

    async def test_deny_dms_only_the_first_time_and_keeps_the_reason_private(self):
        self.make()
        result = await self.cog.deny(MESSAGE_ID, self.reviewer, "  needs more gear  ")
        self.member.send.assert_awaited_once()
        self.assertNotIn("needs more gear", self.member.send.await_args.args[0])
        self.assertEqual(result["decision"]["reason"], "needs more gear")

        # Approved, then corrected to a denial: the role goes, no second DM.
        self.make(applicant_roles=[FRESH_ROLE],
                  records={MESSAGE_ID: record(status="pending", last_decision="approved")})
        await self.cog.deny(MESSAGE_ID, self.reviewer)
        self.member.remove_roles.assert_awaited_once()
        self.member.send.assert_not_awaited()

    async def test_closed_dms_are_reported_next_to_the_card(self):
        self.make()
        self.member.send.side_effect = apply.discord.Forbidden(mock.MagicMock(status=403), "closed")
        await self.cog.approve(MESSAGE_ID, self.reviewer)
        self.channel.send.assert_awaited_once()
        self.assertIn("Couldn't DM", self.channel.send.await_args.args[0])

    async def test_missing_card_still_records_the_decision(self):
        self.make()
        self.channel.fetch_message.side_effect = apply.discord.NotFound(mock.MagicMock(status=404), "gone")
        result = await self.cog.approve(MESSAGE_ID, self.reviewer)
        self.assertEqual(result["status"], "approved")


class HelperTests(unittest.TestCase):
    def test_records_skip_other_keys_and_find_archived_ones(self):
        store = FakeStore({MESSAGE_ID: record(), 1400000000000000000: record(original_message_id=1350000000000000000),
                           "archive_confirmation": {"message_id": 5}})
        self.assertEqual([mid for mid, _ in applications.records(store)], [1400000000000000000, MESSAGE_ID])
        self.assertEqual(applications.find(store, "1350000000000000000")[0], 1400000000000000000)
        with self.assertRaises(ApplicationError):
            applications.find(store, "archive_confirmation")
        with self.assertRaises(ApplicationError):
            applications.find(store, "42")

    def test_default_role(self):
        self.assertEqual(apply.default_role("Priest", "Discipline", {}), "Healer")
        self.assertEqual(apply.default_role("Priest", "Shadow Magic", {}), "DPS")
        self.assertEqual(apply.default_role("Warrior", "Protection", {}), "Tank")
        self.assertEqual(apply.default_role("Druid", "Feral Combat", {"Feral Combat": "tank"}), "Tank")
        self.assertIsNone(apply.default_role("Hunter", "Beast Mastery", {"Beast Mastery": "healer"}))
        self.assertIsNone(apply.default_role("Priest", None, {}))

    def test_screenshot_expiry(self):
        url = "https://cdn.discordapp.com/attachments/1/2/a.png?ex=6930f1a2&is=692fa022&hm=abc"
        self.assertEqual(apply.screenshot_expiry(url), 0x6930F1A2)
        self.assertIsNone(apply.screenshot_expiry("https://example.com/a.png"))

    def test_public_row_has_string_ids(self):
        store = FakeStore({MESSAGE_ID: record(gear={"level": 28, "equipped_item_level": 21, "items": [],
                                                    "empty_slots": ["Neck"], "missing_enchants": [],
                                                    "empty_sockets": 0})})
        row = applications.public_row(store, None, MESSAGE_ID, store.get(MESSAGE_ID))
        self.assertEqual(row["id"], str(MESSAGE_ID))
        self.assertEqual(row["applicant"]["id"], "111")
        self.assertEqual((row["level"], row["item_level"], row["flag_count"]), (28, 21, 1))


class WhoDecidesTests(unittest.TestCase):
    def member(self, role_ids=(), manage_roles=False):
        member = mock.MagicMock(spec=apply.discord.Member)
        member.roles = [role(r) for r in role_ids]
        member.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
        return member

    def test_moderators_and_approvers_decide(self):
        with mock.patch.multiple(config, APPROVER_ROLE_ID=APPROVER_ROLE, MOD_ROLE_ID=800):
            self.assertTrue(apply.ApplyCog.can_decide(self.member([800])))           # moderator role
            self.assertTrue(apply.ApplyCog.can_decide(self.member(manage_roles=True)))  # Manage Roles
            self.assertTrue(apply.ApplyCog.can_decide(self.member([APPROVER_ROLE])))  # approver only
            self.assertFalse(apply.ApplyCog.can_decide(self.member([FRESH_ROLE])))
        with mock.patch.multiple(config, APPROVER_ROLE_ID=0, MOD_ROLE_ID=800):
            self.assertFalse(apply.ApplyCog.can_decide(self.member([0])))  # no approver role set


class SpecChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_offspec_never_offers_the_main_spec(self):
        view = apply.ApplyDialog(1, None, "Priest", ["Discipline", "Holy", "Shadow Magic"],
                                    suggested_spec="Shadow Magic", suggested_role="DPS")
        labels = [o.label for o in view.offspec_select.options]
        self.assertEqual(labels, ["No offspec", "Discipline", "Holy"])
        self.assertEqual([o.label for o in view.spec_select.options if o.default], ["Shadow Magic"])
        self.assertEqual([o.label for o in view.role_select.options], ["Healer", "DPS"])


def with_gallery(*urls):
    """A message as Discord returns a card: its files only in the gallery, not in attachments."""
    gallery = SimpleNamespace(type=apply.discord.ComponentType.media_gallery,
                              items=[SimpleNamespace(media=SimpleNamespace(url=u)) for u in urls])
    return SimpleNamespace(attachments=[], components=[SimpleNamespace(type=None, children=[gallery])])


def upload(name="gear.png", size=1000, content_type="image/png"):
    return SimpleNamespace(filename=name, size=size, content_type=content_type,
                           read=mock.AsyncMock(return_value=b"x" * size))


class InChannelFlowTests(Setup, unittest.IsolatedAsyncioTestCase):
    """Applying happens in the apply channel: no DMs."""

    def setUp(self):
        self._config = mock.patch.multiple(config, **CONFIG, MAX_SCREENSHOTS=2)
        self._config.start()

    def tearDown(self):
        self._config.stop()

    async def test_dialog_status_and_note(self):
        view = apply.ApplyDialog(111, None, "Priest", ["Holy", "Shadow Magic"], suggested_spec="Holy",
                                 suggested_role="Healer", intro="Found **Eggmike Thewhite**!")
        self.assertIn("-# Attached: no note or screenshots (optional)", view.text())
        self.assertEqual([b.label for b in view.children if isinstance(b, apply.discord.ui.Button)],
                         ["Add note & screenshots", "Submit application"])
        view.note, view.uploads = "Raided every tier", [upload()]
        self.assertIn("📝 note (17 characters) · 🖼️ 1 screenshot", view.text())

    async def test_screenshots_are_reattached_within_the_upload_limit(self):
        self.make()
        guild = SimpleNamespace(filesize_limit=2500)
        files, problem = await self.cog._screenshot_files([upload("a.JPG"), upload("c.webp", 1500)], guild)
        self.assertEqual([f.filename for f in files], ["screenshot-1.jpg", "screenshot-2.webp"])
        self.assertIsNone(problem)
        files, problem = await self.cog._screenshot_files([upload(size=2000), upload(size=2000)], guild)
        self.assertEqual(len(files), 1)
        self.assertIn("1 screenshot was too big", problem)
        self.assertEqual(apply.image_uploads([upload(), upload(content_type="text/plain"), upload(), upload()]),
                         [mock.ANY, mock.ANY])  # images only, at most MAX_SCREENSHOTS

    async def test_post_puts_screenshots_on_the_card(self):
        self.make(records={})
        posted = mock.MagicMock()
        posted.id = MESSAGE_ID
        posted.attachments = []
        posted.components = with_gallery("https://cdn.discordapp.com/attachments/1/2/screenshot-1.png").components
        posted.edit = mock.AsyncMock()
        self.channel.send = mock.AsyncMock(return_value=posted)
        interaction = SimpleNamespace(user=SimpleNamespace(id=111), guild=self.guild)
        char_data = {"name": "Eggmike Thewhite", "slug": "eggmike-thewhite", "class_name": "Priest", "level": 28,
                     "guild_name": "Encore", "gear": None}
        with mock.patch.object(apply.bot_messages, "text", lambda key, **kw: f"{key}:{kw}"):
            text = await self.cog._post_application(interaction, char_data, {"role": "DPS", "spec": "Shadow Magic"},
                                                    "Hi", [upload()])
        self.assertIn("application_submitted", text)
        sent = self.channel.send.await_args.kwargs
        self.assertEqual([f.filename for f in sent["files"]], ["screenshot-1.png"])
        [gallery] = [c for c in sent["view"].walk_children() if isinstance(c, apply.discord.ui.MediaGallery)]
        self.assertEqual(gallery.items[0].media.url, "attachment://screenshot-1.png")
        record = self.store.get(MESSAGE_ID)
        self.assertEqual((record["note"], record["screenshot_files"]), ("Hi", ["screenshot-1.png"]))
        self.assertEqual(record["screenshot_urls"],
                         ["https://cdn.discordapp.com/attachments/1/2/screenshot-1.png"])  # for the dashboard

    async def test_archived_cards_keep_their_screenshots(self):
        self.make()
        self.bot.http = SimpleNamespace(get_from_cdn=mock.AsyncMock(side_effect=[b"one", b"two"]))
        card = with_gallery("https://cdn/a.png", "https://cdn/b.png")
        card.id = MESSAGE_ID
        files = await self.cog._card_files(card, {"screenshot_files": ["screenshot-1.png", "screenshot-2.jpg"]})
        self.assertEqual([f.filename for f in files], ["screenshot-1.png", "screenshot-2.jpg"])
        self.assertEqual(apply.card_media_urls(SimpleNamespace(components=None)), [])

    async def test_update_replaces_screenshots_or_only_the_note(self):
        self.make(records={MESSAGE_ID: record(note="old", screenshot_files=["screenshot-1.png"])})
        edited = with_gallery("https://cdn/new-1.png", "https://cdn/new-2.png")
        self.message.guild = SimpleNamespace(filesize_limit=10 ** 7)
        self.message.edit = mock.AsyncMock(return_value=edited)
        text = await self.cog._update_note(MESSAGE_ID, "new note", [upload(), upload("b.png")])
        self.assertIn("note, 2 screenshot(s)", text)
        self.assertEqual(len(self.message.edit.await_args.kwargs["attachments"]), 2)
        stored = self.store.get(MESSAGE_ID)
        self.assertEqual((stored["note"], stored["screenshot_files"], len(stored["screenshot_urls"])),
                         ("new note", ["screenshot-1.png", "screenshot-2.png"], 2))

        self.message.edit.reset_mock()
        await self.cog._update_note(MESSAGE_ID, None, [])  # note removed, screenshots kept
        self.assertNotIn("attachments", self.message.edit.await_args.kwargs)
        self.assertEqual(self.store.get(MESSAGE_ID)["screenshot_files"], ["screenshot-1.png", "screenshot-2.png"])

        self.store.update(MESSAGE_ID, status="approved")
        self.assertIn("decided in the meantime", await self.cog._update_note(MESSAGE_ID, "late", []))


class ApiTests(Setup, DatabaseTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.levels = {"moderator"}

        async def fake_levels(_bot, _user_id, fresh=False):
            return set(self.levels)

        self.make()
        self.bot.latency, self.bot.user = 0.05, None
        self.bot.is_ready, self.bot.is_closed = (lambda: True), (lambda: False)
        self.guild.name, self.guild.icon, self.guild.member_count, self.guild.members = "Test", None, 1, []
        self.cog.approve = mock.AsyncMock(return_value=record(status="approved"))
        self.cog.screenshot_urls = mock.AsyncMock(return_value=[])
        self.bot.get_cog = lambda name: self.cog if name == "ApplyCog" else None
        self._patches = [mock.patch.object(auth, "levels_for", fake_levels),
                         mock.patch.multiple(config, **CONFIG)]
        for p in self._patches:
            p.start()
        self.client = TestClient(TestServer(server.create_app(self.bot)))
        await self.client.start_server()
        token = auth.create_session({"id": "222", "username": "officer"})
        self.client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
        self.headers = {server.CSRF_HEADER: "1"}

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()
        applications._listeners.pop("dashboard", None)

    async def test_moderators_and_approvers_decide(self):
        resp = await self.client.get("/api/applications")
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["counts"], {"pending": 1, "approved": 0, "denied": 0})
        self.assertTrue(body["can_decide"])
        self.assertEqual(body["applications"][0]["id"], str(MESSAGE_ID))

        resp = await self.client.get(f"/api/applications/{MESSAGE_ID}")
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["character"], "Eggmike Thewhite")

        # A moderator without the approver role decides too.
        resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/approve", json={}, headers=self.headers)
        self.assertEqual(resp.status, 200)
        reviewer = self.cog.approve.await_args.args[1]
        self.assertEqual((reviewer.id, reviewer.name, reviewer.via), (222, "officer", "dashboard"))

        self.levels = {"moderator", "approver"}
        resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/approve", json={}, headers=self.headers)
        self.assertEqual(resp.status, 200)

    async def test_a_level_outside_the_deciders_only_reads(self):
        from webapi import applications_api
        with mock.patch.object(applications_api, "DECIDE_LEVELS", {"approver"}):  # as for a future viewer level
            body = await (await self.client.get("/api/applications")).json()
            self.assertFalse(body["can_decide"])
            resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/approve", json={}, headers=self.headers)
            self.assertEqual(resp.status, 403)
        self.cog.approve.assert_not_awaited()

    async def test_without_access_or_csrf_header(self):
        self.levels = set()
        resp = await self.client.get("/api/applications")
        self.assertEqual(resp.status, 403)
        self.levels = {"moderator", "approver"}
        resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/approve", json={})
        self.assertEqual(resp.status, 403)  # no CSRF header
        self.cog.approve.assert_not_awaited()

    async def test_errors_are_plain_messages(self):
        self.levels = {"moderator", "approver"}
        resp = await self.client.post("/api/applications/12345/approve", json={}, headers=self.headers)
        self.assertEqual(resp.status, 404)
        self.cog.approve.side_effect = ApplicationError("This application was already approved.")
        resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/approve", json={}, headers=self.headers)
        self.assertEqual((resp.status, (await resp.json())["error"]), (400, "This application was already approved."))
        resp = await self.client.post(f"/api/applications/{MESSAGE_ID}/delete", json={}, headers=self.headers)
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
