"""
Checks that the bot's credentials work, without printing any secret values.

    python scripts/check_integrations.py

For each service it reports whether the credentials are present (from the
environment or .env) and whether the service accepts them:

  - Discord bot token   -> GET /users/@me, and which servers the bot is in
  - WarcraftLogs keys   -> client-credentials token request
  - Blizzard keys       -> client-credentials token request

Then it checks that every host the bot talks to after logging in can be
reached (the Discord gateway, the WarcraftLogs and Blizzard data APIs,
Wowhead and its icon CDN). A working token proves little if the data host
is blocked by a firewall or proxy. Any HTTP answer counts as reachable.

Exit code 0 when every configured service works and every required host is
reachable, 1 otherwise. Blizzard is optional: missing keys are reported but
don't fail the check. HTTPS_PROXY is honoured, like the bot's own clients
when DISCORD_PROXY is set.
"""

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http_utils import basic_auth_header  # noqa: E402

DISCORD_API = "https://discord.com/api/v10"
WCL_TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
BLIZZARD_TOKEN_URL = "https://oauth.battle.net/oauth/token"

OK, FAIL, SKIP = "OK  ", "FAIL", "SKIP"

# (host, what the bot needs it for, required). Kept in sync with
# wcl_client.py, blizzard_client.py, wowhead.py, icons.py and webapi/auth.py.
HOSTS = [
    ("gateway.discord.gg", "Discord gateway: the bot can't come online without it", True),
    ("cdn.discordapp.com", "Discord images: avatars and emoji in the dashboard", False),
    ("fresh.warcraftlogs.com", "WarcraftLogs data API (reports, rankings)", True),
    ("{region}.api.blizzard.com", "Blizzard data API (characters, items)", False),
    ("render.worldofwarcraft.com", "Blizzard images (item and spell icons)", False),
    ("www.wowhead.com", "Wowhead item/spell lookups (fallback)", False),
    ("wow.zamimg.com", "Wowhead icon images (class, role and spec emoji)", False),
]


async def check_discord(http: aiohttp.ClientSession) -> tuple:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        return FAIL, "DISCORD_TOKEN is not set"
    headers = {"Authorization": f"Bot {token}"}
    async with http.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
        if resp.status == 401:
            return FAIL, "Discord rejected the bot token (reset it in the Developer Portal?)"
        if resp.status != 200:
            return FAIL, f"Discord answered HTTP {resp.status}"
        me = await resp.json()
    async with http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
        guilds = await resp.json() if resp.status == 200 else []
    where = ", ".join(g["name"] for g in guilds) or "no servers yet - invite it with the bot scope"
    return OK, f"logged in as {me['username']} (id {me['id']}); in: {where}"


async def check_client_credentials(http: aiohttp.ClientSession, name: str, url: str,
                                   id_var: str, secret_var: str, required: bool) -> tuple:
    client_id = os.environ.get(id_var, "").strip()
    client_secret = os.environ.get(secret_var, "").strip()
    if not client_id or not client_secret:
        missing = " and ".join(v for v, val in ((id_var, client_id), (secret_var, client_secret)) if not val)
        return (FAIL if required else SKIP), f"{missing} not set"
    async with http.post(
        url,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": basic_auth_header(client_id, client_secret)},
    ) as resp:
        if resp.status in (400, 401, 403):
            return FAIL, f"{name} rejected the client ID/secret (HTTP {resp.status})"
        if resp.status != 200:
            return FAIL, f"{name} answered HTTP {resp.status}"
        data = await resp.json()
    if not data.get("access_token"):
        return FAIL, f"{name} returned no access token"
    return OK, f"access token received (valid {data.get('expires_in', '?')}s)"


async def check_host(http: aiohttp.ClientSession, host: str, required: bool) -> tuple:
    try:
        async with http.get(f"https://{host}/", allow_redirects=False) as resp:
            return OK, f"reachable (HTTP {resp.status})"
    except aiohttp.ClientHttpProxyError as exc:
        return (FAIL if required else SKIP), f"blocked by the proxy (HTTP {exc.status}): allow {host} in the network settings"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return (FAIL if required else SKIP), f"unreachable ({exc.__class__.__name__}): blocked or down"


async def run(check):
    try:
        return await check
    except aiohttp.ClientConnectorError as exc:
        return FAIL, f"couldn't connect to {exc.host} (network blocked or host down)"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return FAIL, f"request failed: {exc.__class__.__name__}"


async def main() -> int:
    load_dotenv()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as http:
        results = [
            ("Discord bot", await run(check_discord(http))),
            ("WarcraftLogs", await run(check_client_credentials(
                http, "WarcraftLogs", WCL_TOKEN_URL, "WCL_CLIENT_ID", "WCL_CLIENT_SECRET", required=True))),
            ("Blizzard API", await run(check_client_credentials(
                http, "Blizzard", BLIZZARD_TOKEN_URL, "BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET",
                required=False))),
        ]
        region = (os.environ.get("SERVER_REGION") or "us").strip().lower()
        hosts = [(h.format(region=region), why, required) for h, why, required in HOSTS]
        reach = await asyncio.gather(*(check_host(http, h, required) for h, _, required in hosts))
    for name, (state, detail) in results:
        print(f"[{state}] {name:<13} {detail}")
    print("\nHosts the bot uses after logging in:")
    for (host, why, required), (state, detail) in zip(hosts, reach):
        print(f"[{state}] {host:<28} {detail}")
        if state != OK:
            print(f"       {'':<28} needed for: {why}{'' if required else ' (optional)'}")
    states = [state for _, (state, _) in results] + [state for state, _ in reach]
    return 0 if FAIL not in states else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
