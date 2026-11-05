"""permissions.py: the shared Discord moderator check."""

import unittest
from types import SimpleNamespace
from unittest import mock

import discord

import config
import permissions


def member(*, manage_roles=False, role_ids=()):
    m = mock.Mock(spec=discord.Member)
    m.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
    m.roles = [SimpleNamespace(id=r) for r in role_ids]
    return m


class IsModeratorTests(unittest.TestCase):
    def setUp(self):
        self._config = mock.patch.object(config, "MOD_ROLE_ID", 500)
        self._config.start()

    def tearDown(self):
        self._config.stop()

    def test_rules(self):
        self.assertTrue(permissions.is_moderator(member(manage_roles=True)))
        self.assertTrue(permissions.is_moderator(member(role_ids=[1, 500])))
        self.assertFalse(permissions.is_moderator(member(role_ids=[1, 2])))
        self.assertFalse(permissions.is_moderator(SimpleNamespace(id=1)))  # a user outside a server

    def test_no_moderator_role_set(self):
        with mock.patch.object(config, "MOD_ROLE_ID", 0):
            self.assertFalse(permissions.is_moderator(member(role_ids=[0])))
            self.assertTrue(permissions.is_moderator(member(manage_roles=True)))


class MemberIsModeratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup(self):
        mod = member(manage_roles=True)
        guild = SimpleNamespace(get_member=lambda uid: mod if uid == 1 else None,
                                fetch_member=mock.AsyncMock(return_value=member(manage_roles=True)))
        self.assertTrue(await permissions.member_is_moderator(guild, 1))
        self.assertTrue(await permissions.member_is_moderator(guild, 2))  # not cached: fetched
        guild.fetch_member.side_effect = discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Member")
        self.assertFalse(await permissions.member_is_moderator(guild, 3))  # not a member
        self.assertFalse(await permissions.member_is_moderator(None, 1))  # not in a server


if __name__ == "__main__":
    unittest.main()
