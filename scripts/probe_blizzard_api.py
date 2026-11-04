"""
Probes Blizzard's API for WoW Forever and writes a report of what answers.

Run it after launch (and again whenever something seems to have changed) to
see which data Blizzard has opened up, then share the report so the bot's
lookups can be adjusted. It never prints or saves the API keys or tokens.

    python scripts/probe_blizzard_api.py
    python scripts/probe_blizzard_api.py --character "Davina Asaria" --guild "Crusader"
    python scripts/probe_blizzard_api.py --login            # also read your own account profile

Inside the test container:

    docker compose exec bot python scripts/probe_blizzard_api.py --character "Davina Asaria"

Options:
  --region eu|us|kr|tw   API region (default: SERVER_REGION, else eu)
  --character "First Surname" or "First"   (repeatable) characters to look up
  --realm SLUG           realm slug for lookups that need one
  --guild NAME           (repeatable) guilds to look up
  --all-realms           also try every player realm for characters and guilds
                         (many requests; use when a realm-based path is expected)
  --login                log in with Battle.net (authorization code flow) to
                         read /profile/user/wow; prints a link, you paste back
                         the address you land on
  --redirect-uri URL     redirect registered for the app (default
                         WEB_BASE_URL + /link/battlenet/callback)
  --user-token TOKEN     use an existing user token instead of --login
  --follow N             follow up to N links found in answers (default 40)
  --out PATH             report file name without extension
                         (default blizzard-probe-<time>; .md and .json are written)

Credentials come from the dashboard's stored integrations when the database
is there, else from BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET in the
environment or .env.

How to read a status (see docs/wow-forever-api.md):
  200  open, with data
  403  the namespace doesn't exist (or isn't open)
  404  the namespace exists but nothing is at this path, or the route doesn't
       exist at all
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from http_utils import basic_auth_header  # noqa: E402

TOKEN_URL = "https://oauth.battle.net/token"
AUTHORIZE_URL = "https://oauth.battle.net/authorize"
VERSIONS_URL = "https://us.version.battle.net/v2/products/wowf/versions"
REGIONS = ("eu", "us", "kr", "tw")
LOCALES = {"eu": "en_GB", "us": "en_US", "kr": "ko_KR", "tw": "zh_TW"}
KEEP_HEADERS = ("battlenet-namespace", "battlenet-schema", "battlenet-schema-revision", "last-modified",
                "cache-control")
LINKS_PER_ANSWER = 2  # a sample of each kind of linked document, not all 26 realms
SERVICE_REALM = re.compile(r"-(web|gmss|partner|account|log|csi)(-\d+)?$")

# Game data worth knowing about for the roster, leveling and raid features.
STATIC_PATHS = [
    "/data/wow/playable-class/index", "/data/wow/playable-specialization/index",
    "/data/wow/playable-race/index", "/data/wow/profession/index", "/data/wow/talent-tree/index",
    "/data/wow/achievement/index", "/data/wow/achievement-category/index", "/data/wow/item-class/index",
    "/data/wow/item-set/index", "/data/wow/journal-instance/index", "/data/wow/journal-expansion/index",
    "/data/wow/journal-encounter/index", "/data/wow/mount/index", "/data/wow/title/index",
    "/data/wow/reputation-faction/index", "/data/wow/guild-crest/index", "/data/wow/power-type/index",
    "/data/wow/media/playable-class/1",
]
DYNAMIC_PATHS = [
    "/data/wow/region/index", "/data/wow/realm/index", "/data/wow/connected-realm/index",
    "/data/wow/mythic-keystone/index", "/data/wow/pvp-season/index", "/data/wow/token/index",
    "/data/wow/auctions/commodities",
]
# Asked on each character that answers 200.
CHARACTER_SUBPATHS = [
    "", "/status", "/equipment", "/specializations", "/professions", "/statistics", "/achievements",
    "/character-media", "/appearance", "/titles", "/reputations", "/collections", "/pvp-summary",
    "/encounters", "/encounters/raids", "/quests/completed", "/hunter-pets",
]
GUILD_SUBPATHS = ["", "/roster", "/activity", "/achievements"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")


def credentials() -> tuple:
    load_dotenv(os.path.join(ROOT, ".env"))
    client_id = client_secret = ""
    try:  # the dashboard's encrypted store, when this runs next to the bot's database
        os.chdir(ROOT)
        import secret_store
        client_id, client_secret = secret_store.get("BLIZZARD_CLIENT_ID"), secret_store.get("BLIZZARD_CLIENT_SECRET")
    except Exception:
        pass
    return (client_id or os.environ.get("BLIZZARD_CLIENT_ID", "").strip(),
            client_secret or os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip())


def default_region() -> str:
    try:
        import config
        return (config.SERVER_REGION or "eu").lower()
    except Exception:
        return "eu"


def default_redirect() -> str:
    base = os.environ.get("WEB_BASE_URL", "http://localhost:8081").rstrip("/")
    return f"{base}/link/battlenet/callback"


class Probe:
    def __init__(self, http: aiohttp.ClientSession, app_token: str, region: str):
        self.http, self.app_token, self.region = http, app_token, region
        self.results: List[dict] = []
        self.seen = set()

    async def get(self, path: str, namespace: str, *, section: str, token: Optional[str] = None,
                  region: Optional[str] = None, quiet: bool = False) -> dict:
        region = region or self.region
        path, _, query = path.partition("?")
        params = {k: v[0] for k, v in parse_qs(query).items()}
        params.update(namespace=namespace, locale=LOCALES.get(region, "en_US"))
        key = (path, namespace, region, bool(token))
        if key in self.seen:
            return next(r for r in self.results if r["key"] == list(key))
        self.seen.add(key)
        started = time.monotonic()
        try:
            async with self.http.get(f"https://{region}.api.blizzard.com{path}", params=params,
                                     headers={"Authorization": f"Bearer {token or self.app_token}"}) as resp:
                status = resp.status
                headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in KEEP_HEADERS}
                body = await resp.json(content_type=None) if status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            status, headers, body = 0, {}, {"error": f"{type(exc).__name__}: {exc}"}
        result = {"section": section, "path": path, "namespace": namespace, "region": region,
                  "user_token": bool(token), "status": status, "ms": round((time.monotonic() - started) * 1000),
                  "headers": headers, "summary": summarize(body) if status == 200 else "", "body": body,
                  "key": list(key)}
        self.results.append(result)
        if not quiet or status == 200:
            print(f"  {status or 'ERR':>3}  {namespace:<28} {path}  {result['summary']}")
        return result

    def links(self, body, namespace_family: str) -> List[tuple]:
        """(path, namespace) for every Blizzard link in an answer."""
        found = []

        def walk(value):
            if isinstance(value, dict):
                href = value.get("href")
                if isinstance(href, str):
                    parsed = urlparse(href if "://" in href else "https:" + href.replace("//./", "//x/", 1))
                    namespace = parse_qs(parsed.query).get("namespace", [""])[0]
                    if namespace_family in namespace and parsed.path.startswith(("/data/wow/", "/profile/")):
                        found.append((parsed.path, namespace))
                for k, v in value.items():
                    if k != "_links":  # the answer's link to itself
                        walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
        walk(body)
        return found


def summarize(body) -> str:
    """A short description of an answer: its keys and list sizes."""
    if not isinstance(body, dict):
        return ""
    parts = []
    for k, v in body.items():
        if k == "_links":
            continue
        if isinstance(v, list):
            parts.append(f"{k}[{len(v)}]")
        elif isinstance(v, (str, int, float, bool)) and len(parts) < 12:
            text = str(v)
            parts.append(f"{k}={text[:40]}")
        else:
            parts.append(k)
    return ", ".join(parts)[:220]


async def app_token(http: aiohttp.ClientSession, client_id: str, client_secret: str) -> str:
    async with http.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                         headers={"Authorization": basic_auth_header(client_id, client_secret)}) as resp:
        if resp.status != 200:
            raise SystemExit(f"Blizzard refused the API keys (HTTP {resp.status}). Check them in the dashboard.")
        return (await resp.json())["access_token"]


async def user_login(http: aiohttp.ClientSession, client_id: str, client_secret: str, redirect: str) -> str:
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urlencode({"client_id": client_id, "scope": "openid wow.profile",
                                           "state": state, "redirect_uri": redirect, "response_type": "code"})
    print("\n1. Open this link in a browser and log in with Battle.net:\n")
    print("   " + url + "\n")
    print(f"2. Blizzard sends you to {redirect}?code=... (the page itself may fail to")
    print("   load; that's fine). Copy the whole address from the address bar and paste it here.")
    print("   (If Blizzard says the redirect is invalid, add it to the app's Redirect URLs")
    print("   at https://develop.battle.net/access/clients first.)\n")
    pasted = input("Address: ").strip()
    query = parse_qs(urlparse(pasted).query)
    if query.get("state", [""])[0] != state:
        raise SystemExit("That address isn't from this login (the state doesn't match). Run it again.")
    if "code" not in query:
        raise SystemExit("No code in that address: " + query.get("error_description", ["unknown error"])[0])
    async with http.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": query["code"][0],
                                          "redirect_uri": redirect, "scope": "openid wow.profile"},
                         headers={"Authorization": basic_auth_header(client_id, client_secret)}) as resp:
        if resp.status != 200:
            raise SystemExit(f"Blizzard didn't accept the login code (HTTP {resp.status}). Run it again.")
        data = await resp.json()
    print(f"Logged in; the token is valid for {round(data.get('expires_in', 0) / 3600)} hours and is not saved.\n")
    return data["access_token"]


async def versions(http: aiohttp.ClientSession) -> dict:
    try:
        async with http.get(VERSIONS_URL) as resp:
            text = await resp.text()
    except aiohttp.ClientError as exc:
        return {"error": str(exc)}
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return {"raw": text[:500]}
    header = [h.split("!")[0] for h in lines[0].split("|")]  # "Region!STRING:0|BuildId!DEC:4|..."
    rows = [dict(zip(header, line.split("|"))) for line in lines[1:]]
    return {r.get("Region", "?"): r.get("VersionsName", "?") for r in rows} or {"raw": text[:500]}


def player_realms(result: dict) -> List[dict]:
    realms = (result.get("body") or {}).get("realms", []) if result["status"] == 200 else []
    return [r for r in realms if r.get("slug") and not SERVICE_REALM.search(r["slug"])]


async def run(args) -> dict:
    client_id, client_secret = credentials()
    if not client_id or not client_secret:
        raise SystemExit("No Blizzard API keys: add them in the dashboard (Settings, Integrations) "
                         "or set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET.")
    region = args.region
    ns = {kind: f"{kind}-forever-{region}" for kind in ("static", "dynamic", "profile")}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as http:
        token = await app_token(http, client_id, client_secret)
        user_token = args.user_token or (await user_login(http, client_id, client_secret, args.redirect_uri)
                                         if args.login else None)
        probe = Probe(http, token, region)

        print("Client versions (version server):")
        build = await versions(http)
        for k, v in build.items():
            print(f"  {k}: {v}")

        print("\nNamespaces per region (403 = doesn't exist, 404/200 = exists):")
        for r in REGIONS:
            await probe.get("/data/wow/region/index", f"dynamic-forever-{r}", section="namespaces", region=r)
            await probe.get("/data/wow/playable-class/index", f"static-forever-{r}", section="namespaces", region=r)
            await probe.get("/profile/wow/character/probe-test", f"profile-forever-{r}", section="namespaces",
                            region=r)
        await probe.get("/data/wow/realm/index", f"dynamic-bogus-{region}", section="namespaces")

        print("\nDynamic data:")
        realm_index = None
        for path in DYNAMIC_PATHS:
            result = await probe.get(path, ns["dynamic"], section="dynamic")
            if path == "/data/wow/realm/index":
                realm_index = result
        realms = player_realms(realm_index or {"status": 0})
        if realms:
            first = realms[0]
            await probe.get(f"/data/wow/realm/{first['slug']}", ns["dynamic"], section="dynamic")
            await probe.get(f"/data/wow/connected-realm/{first['id']}", ns["dynamic"], section="dynamic")
        print(f"  ({len(realms)} player realms)")

        print("\nStatic data:")
        for path in STATIC_PATHS:
            await probe.get(path, ns["static"], section="static")

        realm_slugs = [slug(args.realm)] if args.realm else []
        if args.all_realms:
            realm_slugs += [r["slug"] for r in realms if r["slug"] not in realm_slugs]

        characters_found = []
        if args.character:
            print("\nCharacters:")
        for text in args.character or []:
            first, _, surname = text.strip().partition(" ")
            first, surname = slug(first), slug(surname)
            candidates = [f"/profile/wow/character/{first}-{surname}" if surname else None,
                          f"/profile/wow/character/{first}" if not surname else None]
            for realm_slug in realm_slugs:
                candidates.append(f"/profile/wow/character/{realm_slug}/{first}-{surname}" if surname else None)
                candidates.append(f"/profile/wow/character/{realm_slug}/{first}")
            for path in filter(None, candidates):
                result = await probe.get(path, ns["profile"], section="characters", quiet=len(realm_slugs) > 3)
                if result["status"] == 200:
                    characters_found.append(path)
                    break
        for base in characters_found:
            for sub in CHARACTER_SUBPATHS[1:]:
                await probe.get(base + sub, ns["profile"], section="characters")

        if args.guild:
            print("\nGuilds:")
        for name in args.guild or []:
            for realm_slug in realm_slugs or [None]:
                path = f"/data/wow/guild/{realm_slug}/{slug(name)}" if realm_slug else f"/data/wow/guild/{slug(name)}"
                result = await probe.get(path, ns["profile"], section="guilds", quiet=len(realm_slugs) > 3)
                if result["status"] == 200:
                    for sub in GUILD_SUBPATHS[1:]:
                        await probe.get(path + sub, ns["profile"], section="guilds")
                    break

        if user_token:
            print("\nYour account profile (user token):")
            account = await probe.get("/profile/user/wow", ns["profile"], section="account", token=user_token)
            if account["status"] == 200:
                for path, namespace in probe.links(account["body"], "forever")[:args.follow]:
                    use_user = path.startswith("/profile/user/")
                    await probe.get(path, namespace, section="account", token=user_token if use_user else None)
                    for sub in CHARACTER_SUBPATHS[1:] if path.startswith("/profile/wow/character/") else []:
                        await probe.get(path + sub, namespace, section="account", quiet=True)
            elif account["status"] == 404:
                print("  404: no Forever characters on this account yet (log into the game and out again).")

        if args.follow:
            print(f"\nFollowing links found in the answers (up to {args.follow}):")
            followed = 0
            for result in list(probe.results):
                if result["status"] != 200 or result["section"] in ("namespaces", "account"):
                    continue
                for path, namespace in probe.links(result["body"], "forever")[:LINKS_PER_ANSWER]:
                    if followed >= args.follow:
                        break
                    if (path, namespace, region, False) in probe.seen:
                        continue
                    followed += 1
                    await probe.get(path, namespace, section="links", quiet=True)

    return {"when": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "region": region,
            "versions": build, "results": probe.results}


def markdown(report: dict) -> str:
    lines = [f"# Blizzard API probe ({report['when']}, region {report['region']})", "",
             "Client versions: " + ", ".join(f"{k} {v}" for k, v in report["versions"].items()), ""]
    sections = {"namespaces": "Namespaces", "dynamic": "Dynamic data", "static": "Static data",
                "characters": "Characters", "guilds": "Guilds", "account": "Account profile (user token)",
                "links": "Links found in answers"}
    for key, title in sections.items():
        rows = [r for r in report["results"] if r["section"] == key]
        if not rows:
            continue
        if key in ("links", "characters", "guilds"):
            shown = [r for r in rows if r["status"] == 200]
            hidden = len(rows) - len(shown)
            rows = shown or rows[:5]
        else:
            hidden = 0
        lines += [f"## {title}", "", "| Status | Namespace | Path | Served namespace | What's in it |",
                  "|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['status']} | `{r['namespace']}` | `{r['path']}` | "
                         f"{r['headers'].get('battlenet-namespace', '')} | {r['summary'].replace('|', '/')} |")
        if hidden:
            lines.append(f"\n{hidden} other attempts answered something else; see the .json file.")
        lines.append("")
    opened = [r for r in report["results"] if r["status"] == 200 and r["section"] in ("characters", "guilds", "account")]
    for r in opened[:6]:
        body = json.dumps(r["body"], indent=2, ensure_ascii=False)
        lines += [f"### `{r['path']}`", "", "```json", body[:3000] + ("\n..." if len(body) > 3000 else ""), "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Probe Blizzard's WoW Forever API and write a report.")
    parser.add_argument("--region", choices=REGIONS, default=None)
    parser.add_argument("--character", action="append", help='"First Surname" (repeatable)')
    parser.add_argument("--realm")
    parser.add_argument("--guild", action="append")
    parser.add_argument("--all-realms", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--redirect-uri", default=None)
    parser.add_argument("--user-token")
    parser.add_argument("--follow", type=int, default=40)
    parser.add_argument("--out")
    args = parser.parse_args()
    args.region = args.region or default_region()
    args.redirect_uri = args.redirect_uri or default_redirect()
    out = args.out or "blizzard-probe-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")

    report = asyncio.run(run(args))
    for r in report["results"]:
        r.pop("key", None)
    with open(out + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    with open(out + ".md", "w", encoding="utf-8") as f:
        f.write(markdown(report))
    opened = sum(r["status"] == 200 for r in report["results"])
    print(f"\n{opened} of {len(report['results'])} requests answered 200.")
    print(f"Report: {os.path.abspath(out)}.md (summary) and .json (full answers)")


if __name__ == "__main__":
    main()
