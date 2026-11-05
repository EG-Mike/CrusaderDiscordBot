"""
The bot's library of WoW icon emoji: one bot-owned ("application") emoji
per icon image, reused by every item and spell that shows that icon.

Many items and spells share an icon - e.g. Flash Heal and Flash of Light
both use icon file 135907 (spell_holy_flashheal) - and a bot can own at
most MAX_APP_EMOJI emoji, so emoji are keyed by the icon, not by the
item/spell:

  emoji_library   one row per uploaded icon: Discord emoji ID and name, plus
                  how the icon is identified - Blizzard's icon file ID
                  (`file_id`) and/or the icon's name (`icon_name`).
  emoji_aliases   which items/spells use which row (kind + game ID + name).

Where icons come from, in order:
  1. Blizzard's Game Data API (bot.blizzard): the item/spell's name and its
     icon (file ID + image on Blizzard's own image server), trying each
     namespace in EMOJI_BLIZZARD_NAMESPACES (e.g. classic, then modern).
  2. Only if Blizzard has no record of it (e.g. a spell removed from the
     modern game) and EMOJI_WOWHEAD_FALLBACK is on: Wowhead's tooltip
     service (name + icon name), for the EMOJI_WOWHEAD_DATA_ENV game version.

Used by the dashboard's Emoji page and editor emoji picker
(webapi/emoji_api.py), and raid summaries'
loot/ability icons (icons.ensure_item_emoji / ensure_spell_emoji).
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord

import config
import db

log = logging.getLogger("wow-apply-bot.emoji_library")

MAX_APP_EMOJI = 2000          # Discord's limit of emoji per bot application
KINDS = ("item", "spell")
SEARCH_LIMIT_PER_KIND = 12
CACHE_SECONDS = 60
WOWHEAD_TOOLTIP_URL = "https://nether.wowhead.com/tooltip/{kind}/{id}"
WOWHEAD_ICON_URL = "https://wow.zamimg.com/images/wow/icons/large/{icon}.jpg"
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
}

# wowhead.com/item=19019/..., /tbc/spell=2061, "item:19019", "spell 2061"
_REFERENCE_RE = re.compile(r"\b(item|spell)\s*[=: ]\s*(\d{1,9})\b", re.IGNORECASE)
_EMOJI_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")


class EmojiError(Exception):
    """Something the person can fix or should know; the message says what."""


@dataclass
class Resolved:
    """An item or spell with its icon."""
    kind: str
    game_id: int
    name: str
    image_url: str
    file_id: Optional[int]
    icon_name: Optional[str]
    source: str  # "blizzard" | "wowhead"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "game_id": self.game_id, "name": self.name, "image_url": self.image_url,
                "file_id": self.file_id, "icon_name": self.icon_name, "source": self.source}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emoji_text(row: dict) -> str:
    """How to write the emoji in a Discord message."""
    return f"<{'a' if row['animated'] else ''}:{row['emoji_name']}:{row['emoji_id']}>"


def emoji_image_url(emoji_id, animated=False) -> str:
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'webp'}?size=64"


def parse_references(text: str) -> List[Tuple[str, int]]:
    """Item/spell references in pasted text (Wowhead links or 'item:123'),
    in order, without duplicates. Only the ID is used, never the site."""
    seen = []
    for kind, game_id in _REFERENCE_RE.findall(text or ""):
        ref = (kind.lower(), int(game_id))
        if ref not in seen:
            seen.append(ref)
    return seen


def icon_from_url(url: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """(file_id, icon_name) from an icon image URL: Blizzard's modern
    images are named by file ID (.../56/135907.jpg), classic ones and
    Wowhead's by icon name (.../inv_sword_39.jpg)."""
    if not url:
        return None, None
    stem = url.rsplit("/", 1)[-1].split("?", 1)[0].rsplit(".", 1)[0].lower()
    if stem.isdigit():
        return int(stem), None
    return None, stem or None


# --- looking items and spells up -----------------------------------------------------------

def _namespaces() -> List[str]:
    value = getattr(config, "EMOJI_BLIZZARD_NAMESPACES", ["static-classic", "static"])
    return [ns for ns in (value if isinstance(value, (list, tuple)) else [value]) if ns] or ["static"]


def _localized(value) -> str:
    if isinstance(value, dict):
        return value.get("en_US") or next(iter(value.values()), "")
    return value or ""


def _icon_asset(media: Optional[dict]) -> Tuple[Optional[str], Optional[int]]:
    for asset in (media or {}).get("assets") or []:
        if asset.get("key") == "icon" and asset.get("value"):
            return asset["value"], asset.get("file_data_id")
    return None, None


async def _from_blizzard(bot, kind: str, game_id: int) -> Optional[Resolved]:
    blizzard = getattr(bot, "blizzard", None)
    if blizzard is None:
        return None
    for namespace in _namespaces():
        record = await blizzard.get_game_data(f"{kind}/{game_id}", namespace)
        if record is None:
            continue
        media = await blizzard.get_game_data(f"media/{kind}/{game_id}", namespace)
        url, file_id = _icon_asset(media)
        if not url:
            continue
        url_file_id, icon_name = icon_from_url(url)
        return Resolved(kind, game_id, _localized(record.get("name")) or f"{kind.title()} {game_id}", url,
                        file_id or url_file_id, icon_name, "blizzard")
    return None


async def _from_wowhead(kind: str, game_id: int) -> Optional[Resolved]:
    if not getattr(config, "EMOJI_WOWHEAD_FALLBACK", True):
        return None
    params = {}
    data_env = getattr(config, "EMOJI_WOWHEAD_DATA_ENV", None)
    if data_env:
        params["dataEnv"] = str(data_env)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as http:
        async with http.get(WOWHEAD_TOOLTIP_URL.format(kind=kind, id=game_id), params=params,
                            headers=_BROWSER_HEADERS) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    icon = (data.get("icon") or "").lower()
    if not icon or not data.get("name"):
        return None
    return Resolved(kind, game_id, data["name"], WOWHEAD_ICON_URL.format(icon=icon), None, icon, "wowhead")


async def resolve(bot, kind: str, game_id: int) -> Resolved:
    """An item/spell's name and icon: Blizzard first, Wowhead as fallback.

    Blizzard's modern images are named by file ID only, so the icon's name
    is then asked from Wowhead (when allowed) - just the name, to match
    icons that only Wowhead knows (e.g. Amplify Magic shares Flash Heal's
    spell_holy_flashheal) and to give the emoji a readable name."""
    if kind not in KINDS:
        raise EmojiError("Only items and spells have icons.")
    errors = []
    try:
        found = await _from_blizzard(bot, kind, game_id)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        errors.append(exc)
        found = None
    if found is not None:
        if not found.icon_name:
            try:
                hint = await _from_wowhead(kind, game_id)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                hint = None
            if hint is not None:
                found.icon_name = hint.icon_name
        return found
    try:
        found = await _from_wowhead(kind, game_id)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        errors.append(exc)
    if found is not None:
        return found
    if errors:
        log.warning("Looking up %s %s failed: %s", kind, game_id, errors)
        raise EmojiError(f"Couldn't look up {kind} {game_id} right now: Blizzard's API didn't answer. Try again shortly.")
    if getattr(bot, "blizzard", None) is None:
        raise EmojiError("Blizzard API keys aren't set up (Settings, Integrations), so icons can't be looked up.")
    raise EmojiError(f"No {kind} with ID {game_id} was found.")


async def search(bot, query: str) -> List[dict]:
    """Items and spells for a name, or for pasted links/IDs, each with its
    icon and whether it's already in the library."""
    refs = parse_references(query)
    if refs:
        found = []
        for kind, game_id in refs[:SEARCH_LIMIT_PER_KIND]:
            try:
                found.append((await resolve(bot, kind, game_id)).as_dict())
            except EmojiError as exc:
                found.append({"kind": kind, "game_id": game_id, "error": str(exc)})
        return [_with_library_state(r) for r in found]

    name = (query or "").strip()
    if len(name) < 2:
        return []
    blizzard = getattr(bot, "blizzard", None)
    if blizzard is None:
        raise EmojiError("Search needs Blizzard API keys (Settings, Integrations). Pasting a Wowhead link works without.")

    # One connection for the whole search (many small requests).
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def find(kind, namespace):
            try:
                return await blizzard.get_game_data(f"search/{kind}", namespace,
                                                    {"name.en_US": name, "_pageSize": SEARCH_LIMIT_PER_KIND}, session)
            except aiohttp.ClientError:
                return None

        searches = [(kind, ns) for kind in KINDS for ns in _namespaces()]
        pages = await asyncio.gather(*(find(kind, ns) for kind, ns in searches))
        candidates: Dict[Tuple[str, int], Tuple[str, str]] = {}
        for (kind, namespace), page in zip(searches, pages):  # namespace order = preference
            for result in (page or {}).get("results") or []:
                data = result.get("data") or {}
                key = (kind, int(data.get("id", 0)))
                if key[1] and key not in candidates:
                    candidates[key] = (_localized(data.get("name")), namespace)
        needle = name.lower()
        ranked = sorted(candidates.items(), key=lambda kv: (kv[1][0].lower() != needle,
                                                           not kv[1][0].lower().startswith(needle), kv[1][0]))
        ranked = ranked[:SEARCH_LIMIT_PER_KIND * 2]

        gate = asyncio.Semaphore(6)

        async def with_icon(key, value):
            (kind, game_id), (label, namespace) = key, value
            async with gate:
                try:
                    media = await blizzard.get_game_data(f"media/{kind}/{game_id}", namespace, session=session)
                except aiohttp.ClientError:
                    media = None
            url, file_id = _icon_asset(media)
            if not url:
                return None
            url_file_id, icon_name = icon_from_url(url)
            return Resolved(kind, game_id, label, url, file_id or url_file_id, icon_name, "blizzard").as_dict()
        results = await asyncio.gather(*(with_icon(k, v) for k, v in ranked))
    return [_with_library_state(r) for r in results if r]


# --- the library ------------------------------------------------------------------------------

def _row(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def find_icon(file_id: Optional[int], icon_name: Optional[str]) -> Optional[dict]:
    """The library entry for an icon, matched on file ID or icon name."""
    if file_id:
        rows = db.query("SELECT * FROM emoji_library WHERE file_id = ?", (int(file_id),))
        if rows:
            return _row(rows[0])
    return _find_by_name(icon_name)


def _find_by_name(icon_name: Optional[str]) -> Optional[dict]:
    if icon_name:
        rows = db.query("SELECT * FROM emoji_library WHERE icon_name = ?", (icon_name.lower(),))
        if rows:
            return _row(rows[0])
    return None


def find_alias(kind: str, game_id: int) -> Optional[dict]:
    rows = db.query("SELECT l.* FROM emoji_aliases a JOIN emoji_library l ON l.id = a.library_id "
                    "WHERE a.kind = ? AND a.game_id = ?", (kind, int(game_id)))
    return _row(rows[0]) if rows else None


def _with_library_state(result: dict) -> dict:
    if result.get("error"):
        return result
    entry = find_alias(result["kind"], result["game_id"])
    state = "added" if entry else None
    if entry is None:
        entry = find_icon(result.get("file_id"), result.get("icon_name"))
        state = "shared" if entry else "new"
    out = {**result, "state": state}
    if entry:
        out["emoji"] = _public(entry, aliases=_aliases(entry["id"]))
    return out


def _aliases(library_id: int) -> List[dict]:
    rows = db.query("SELECT kind, game_id, name FROM emoji_aliases WHERE library_id = ? ORDER BY added_at, rowid",
                    (library_id,))
    return [dict(r) for r in rows]


def _public(row: dict, aliases: Optional[List[dict]] = None) -> dict:
    return {
        "id": row["id"], "emoji_id": str(row["emoji_id"]), "name": row["emoji_name"], "animated": bool(row["animated"]),
        "text": emoji_text(row), "url": emoji_image_url(row["emoji_id"], row["animated"]),
        "file_id": row["file_id"], "icon_name": row["icon_name"], "source": row["source"],
        "created_at": row["created_at"], "created_by": row["created_by"],
        "aliases": aliases if aliases is not None else _aliases(row["id"]),
    }


def library() -> List[dict]:
    rows = db.query("SELECT * FROM emoji_library ORDER BY emoji_name")
    aliases: Dict[int, List[dict]] = {}
    for a in db.query("SELECT library_id, kind, game_id, name FROM emoji_aliases ORDER BY added_at, rowid"):
        aliases.setdefault(a["library_id"], []).append({"kind": a["kind"], "game_id": a["game_id"], "name": a["name"]})
    return [_public(dict(r), aliases.get(r["id"], [])) for r in rows]


def _add_alias(library_id: int, kind: str, game_id: int, name: str, by: str):
    db.execute("INSERT INTO emoji_aliases (kind, game_id, name, library_id, added_at, added_by) VALUES (?, ?, ?, ?, ?, ?) "
               "ON CONFLICT(kind, game_id) DO UPDATE SET name = excluded.name, library_id = excluded.library_id",
               (kind, int(game_id), name, library_id, _now(), by))


def _learn(entry: dict, resolved: Resolved):
    """Fill in whichever identifier (file ID / icon name) an entry was
    missing, so later lookups from either source find it."""
    if not entry["file_id"] and resolved.file_id:
        db.execute("UPDATE emoji_library SET file_id = ? WHERE id = ?", (resolved.file_id, entry["id"]))
    if not entry["icon_name"] and resolved.icon_name:
        db.execute("UPDATE emoji_library SET icon_name = ? WHERE id = ?", (resolved.icon_name.lower(), entry["id"]))


def _insert_library(emoji: discord.Emoji, resolved: Resolved, by: str, source: str) -> dict:
    cursor = db.execute(
        "INSERT INTO emoji_library (emoji_id, emoji_name, animated, file_id, icon_name, image_url, source, created_at, "
        "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(emoji.id), emoji.name, int(bool(emoji.animated)), resolved.file_id,
         resolved.icon_name.lower() if resolved.icon_name else None, resolved.image_url, source, _now(), by),
    )
    return _row(db.query("SELECT * FROM emoji_library WHERE id = ?", (cursor.lastrowid,))[0])


# --- the bot's emoji in Discord ---------------------------------------------------------------

_app_emoji: Dict[str, discord.Emoji] = {}
_app_emoji_loaded = 0.0
_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def app_emoji(bot, *, fresh: bool = False) -> Dict[str, discord.Emoji]:
    """The bot's own emoji by ID (cached briefly). Also forgets library
    entries whose emoji was deleted outside the bot."""
    global _app_emoji_loaded
    if fresh or not _app_emoji_loaded or time.monotonic() - _app_emoji_loaded > CACHE_SECONDS:
        emojis = await bot.fetch_application_emojis()
        _app_emoji.clear()
        _app_emoji.update({str(e.id): e for e in emojis})
        _app_emoji_loaded = time.monotonic()
        gone = [r["id"] for r in db.query("SELECT id, emoji_id FROM emoji_library") if r["emoji_id"] not in _app_emoji]
        for library_id in gone:
            db.execute("DELETE FROM emoji_library WHERE id = ?", (library_id,))
        if gone:
            log.info("Emoji library: forgot %d emoji deleted outside the bot", len(gone))
    return _app_emoji


def _unique_name(base: str, taken: set) -> str:
    """A valid emoji name (2-32 letters, digits, _) not used by another emoji."""
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "icon"
    if len(slug) < 2:
        slug = f"{slug}_icon"
    if len(slug) > 32:
        slug = f"{slug[:25]}_{hashlib.sha1(slug.encode()).hexdigest()[:6]}"
    name, n = slug, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = slug[:32 - len(suffix)] + suffix, n + 1
    return name


async def _download(url: str) -> bytes:
    """The image, trying Blizzard's fallback addresses for the icon (wow_api.icon_urls)."""
    import wow_api

    data, last_error = None, None
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=15)) as http:
        for candidate in wow_api.icon_urls(url) or [url]:
            try:
                async with http.get(candidate, headers=_BROWSER_HEADERS) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                    break
            except aiohttp.ClientResponseError as exc:
                last_error = exc
    if data is None:
        raise last_error
    if len(data) > 256 * 1024:
        raise EmojiError("The icon image is larger than Discord allows for an emoji (256 KB).")
    return data


async def add(bot, kind: str, game_id: int, by: str, *, fallback_image_url: Optional[str] = None) -> dict:
    """Makes sure an item/spell has an emoji, reusing the emoji of its icon
    when one exists. Returns {"status": "exists" | "shared" | "created",
    "emoji": {...}, "item": {...}}. Raises EmojiError."""
    async with _get_lock():
        existing = find_alias(kind, game_id)
        if existing is not None and existing["emoji_id"] in await app_emoji(bot):
            return {"status": "exists", "emoji": _public(existing), "item": {"kind": kind, "game_id": game_id}}

        try:
            resolved = await resolve(bot, kind, game_id)
        except EmojiError:
            if not fallback_image_url:
                raise
            file_id, icon_name = icon_from_url(fallback_image_url)
            resolved = Resolved(kind, game_id, f"{kind.title()} {game_id}", fallback_image_url, file_id, icon_name,
                                "given")

        emojis = await app_emoji(bot)
        entry = find_icon(resolved.file_id, resolved.icon_name)
        if entry is not None and entry["emoji_id"] in emojis:
            _learn(entry, resolved)
            _add_alias(entry["id"], kind, game_id, resolved.name, by)
            return {"status": "shared", "emoji": _public(entry), "item": resolved.as_dict()}

        # A bot emoji the library doesn't know yet but that is clearly this
        # icon - named after the icon, or made per item/spell by earlier
        # versions ("item_19019") - is adopted instead of uploading the same
        # image again (e.g. after the database was reset).
        known = {r["emoji_id"] for r in db.query("SELECT emoji_id FROM emoji_library")}
        wanted = {f"{kind}_{game_id}", (resolved.icon_name or "").lower()} - {""}
        legacy = next((e for e in emojis.values() if e.name.lower() in wanted and str(e.id) not in known), None)
        if legacy is not None:
            entry = _insert_library(legacy, resolved, by, "adopted")
            _add_alias(entry["id"], kind, game_id, resolved.name, by)
            return {"status": "shared", "emoji": _public(entry), "item": resolved.as_dict()}

        if len(emojis) >= MAX_APP_EMOJI:
            raise EmojiError(f"The bot already has {MAX_APP_EMOJI} emoji, the most Discord allows. "
                             "Delete some unused ones on the Emoji page first.")
        try:
            image = await _download(resolved.image_url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise EmojiError(f"Couldn't download the icon for {resolved.name}.") from None
        name = _unique_name(resolved.icon_name or resolved.name, {e.name for e in emojis.values()})
        try:
            emoji = await bot.create_application_emoji(name=name, image=image)
        except discord.HTTPException as exc:
            raise EmojiError(f"Discord refused the emoji for {resolved.name}: {exc.text or exc.status}") from None
        emojis[str(emoji.id)] = emoji
        entry = _insert_library(emoji, resolved, by, resolved.source)
        _add_alias(entry["id"], kind, game_id, resolved.name, by)
        log.info("Emoji library: added :%s: for %s %s (%s)", name, kind, game_id, resolved.name)
        return {"status": "created", "emoji": _public(entry), "item": resolved.as_dict()}


async def ensure_icon(bot, kind: str, game_id: int, image_url: Optional[str] = None, by: str = "automatic") -> str:
    """The emoji text for an item/spell's icon, creating or reusing the
    emoji as needed; '' if that isn't possible. Never raises - for features
    that just want to decorate a line with an icon."""
    existing = find_alias(kind, game_id)
    if existing is not None and existing["emoji_id"] in _app_emoji:
        return emoji_text(existing)
    try:
        return (await add(bot, kind, game_id, by, fallback_image_url=image_url))["emoji"]["text"]
    except EmojiError as exc:
        log.info("No emoji for %s %s: %s", kind, game_id, exc)
    except Exception:
        log.warning("No emoji for %s %s", kind, game_id, exc_info=True)
    return ""


async def delete(bot, library_id: int) -> dict:
    rows = db.query("SELECT * FROM emoji_library WHERE id = ?", (int(library_id),))
    if not rows:
        raise EmojiError("That emoji isn't in the library (any more).")
    entry = dict(rows[0])
    emojis = await app_emoji(bot)
    emoji = emojis.get(entry["emoji_id"])
    if emoji is not None:
        try:
            await emoji.delete()
        except discord.HTTPException as exc:
            raise EmojiError(f"Discord refused to delete it: {exc.text or exc.status}") from None
        emojis.pop(entry["emoji_id"], None)
    db.execute("DELETE FROM emoji_library WHERE id = ?", (entry["id"],))
    return entry


async def overview(bot) -> dict:
    """Everything the dashboard shows: the library, the bot's other emoji
    (e.g. class and spec icons), and how many of the bot's slots are used."""
    emojis = await app_emoji(bot)
    entries = library()
    in_library = {e["emoji_id"] for e in entries}
    others = [{"emoji_id": str(e.id), "name": e.name, "animated": e.animated, "url": emoji_image_url(e.id, e.animated),
               "text": str(e)} for e in sorted(emojis.values(), key=lambda e: e.name) if str(e.id) not in in_library]
    return {"library": entries, "other": others, "used": len(emojis), "limit": MAX_APP_EMOJI}
