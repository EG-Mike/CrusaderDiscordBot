"""
Web dashboard: starts webapi/server.py's HTTP server inside the bot process.

Only loads when config.FEATURE_WEB_DASHBOARD_ENABLED is True (off by
default - see the README's "Web dashboard" section). The server runs as
its own task: if it fails to start (e.g. the port is taken) the error is
logged and the bot carries on without it - the dashboard must never take
the bot down.

.env settings:
  DISCORD_CLIENT_SECRET   OAuth2 client secret (Developer Portal -> OAuth2)
  WEB_BASE_URL            public address, e.g. http://localhost:8081 while
                          testing over an SSH tunnel, https://bot.example.com live
  WEB_HOST / WEB_PORT     where to listen (default 127.0.0.1:8081)
  WEB_ADMIN_USER_IDS      comma-separated Discord user IDs with admin access
"""

import logging
import os

from aiohttp import web
from discord.ext import commands

import db
import system_log
from webapi import auth
from webapi.common import HUB_KEY
from webapi.server import create_app

log = logging.getLogger("wow-apply-bot.web")


class WebDashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.runner = None

    async def cog_load(self):
        if not auth.client_secret():
            log.warning("Web dashboard: DISCORD_CLIENT_SECRET isn't set - nobody will be able to log in.")
        db.connection()  # create the database/tables up front
        host = os.environ.get("WEB_HOST", "127.0.0.1")
        port = int(os.environ.get("WEB_PORT", "8081"))
        try:
            self.runner = web.AppRunner(create_app(self.bot), access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner, host, port).start()
        except Exception:
            log.exception("Web dashboard failed to start on %s:%s - the bot keeps running without it", host, port)
            if self.runner is not None:
                await self.runner.cleanup()
                self.runner = None
            return
        log.info("Web dashboard listening on http://%s:%s (public address: %s)", host, port, auth.base_url())
        system_log.web_started(auth.base_url())

    async def cog_unload(self):
        if self.runner is not None:
            app = self.runner.app
            await self.runner.cleanup()
            self.runner = None
            system_log.web_stopped(app[HUB_KEY].closed_on_shutdown)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebDashboardCog(bot))
