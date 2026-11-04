"""
External services (WarcraftLogs, Blizzard): API keys entered in the
dashboard, a "test connection" check, and applying saved keys to the
running bot straight away (both clients only use their keys when fetching
an access token, so no restart is needed).
"""

import aiohttp

import config
import secret_store
from blizzard_client import OAUTH_URL as BLIZZARD_TOKEN_URL, BlizzardClient
from wcl_client import TOKEN_URL as WCL_TOKEN_URL

TOKEN_URLS = {"warcraftlogs": WCL_TOKEN_URL, "blizzard": BLIZZARD_TOKEN_URL}


def list_services() -> list:
    return [
        {
            "id": service,
            "label": label,
            "required": required,
            "configured": secret_store.configured(service),
            "fields": [secret_store.describe(key) for key in keys],
        }
        for service, (label, required, keys) in secret_store.SERVICES.items()
    ]


async def test(service: str, client_id: str, client_secret: str) -> tuple:
    """Fetches an access token with these keys: (ok, message)."""
    if not client_id or not client_secret:
        return False, "Enter both the client ID and the client secret."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.post(
                TOKEN_URLS[service],
                data={"grant_type": "client_credentials"},
                headers={"Authorization": aiohttp.BasicAuth(client_id, client_secret).encode()},
            ) as resp:
                if resp.status in (400, 401, 403):
                    return False, "The service rejected these keys. Check the client ID and secret."
                if resp.status != 200:
                    return False, f"The service answered with an error (HTTP {resp.status}). Try again later."
                data = await resp.json()
                if not data.get("access_token"):
                    return False, "Unexpected answer from the service."
    except aiohttp.ClientError as exc:
        return False, f"Couldn't reach the service: {exc.__class__.__name__}"
    except TimeoutError:
        return False, "The service didn't answer in time."
    return True, "Connected."


def apply_to_bot(bot):
    """Point the running bot's API clients at the currently saved keys."""
    bot.wcl.client_id = secret_store.get("WCL_CLIENT_ID")
    bot.wcl.client_secret = secret_store.get("WCL_CLIENT_SECRET")
    bot.wcl._token = None
    bot.wcl._token_expires_at = 0

    blizzard_id = secret_store.get("BLIZZARD_CLIENT_ID")
    blizzard_secret = secret_store.get("BLIZZARD_CLIENT_SECRET")
    if blizzard_id and blizzard_secret:
        if bot.blizzard is None:
            bot.blizzard = BlizzardClient(blizzard_id, blizzard_secret, region=config.SERVER_REGION)
        else:
            bot.blizzard.client_id = blizzard_id
            bot.blizzard.client_secret = blizzard_secret
            bot.blizzard._token = None
            bot.blizzard._token_expires_at = 0
    else:
        bot.blizzard = None
