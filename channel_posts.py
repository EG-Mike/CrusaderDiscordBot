"""
Messages the bot keeps posted in a channel and edits in place (the guild
roster, the crafting channel): which messages they are and a hash of their
content are kept in the database (db meta), so they're only edited when
the content changed, re-posted in order when someone deleted one, and moved
when the channel setting changes.

A feature builds its views (one per message) and a digest of what they
show, then calls publish().
"""

import asyncio
import json
import logging
from typing import List, Optional

import discord

import db

log = logging.getLogger("wow-apply-bot.channel_posts")

TEXT_LIMIT = 3800  # per message: Discord allows 4000 characters of text
NO_PINGS = discord.AllowedMentions.none()


def chunks(header: str, lines: List[str], limit: int = TEXT_LIMIT) -> List[str]:
    """The text split over messages at line breaks, each within `limit`."""
    found, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            found.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    found.append(current)
    return found


def state(meta_key: str) -> dict:
    """{"channel_id", "message_ids", "hash", "updated"} of what's posted."""
    return json.loads(db.get_meta(meta_key) or "{}") if db.exists() else {}


def is_current(posted: dict, channel_id: int, digest: str) -> bool:
    return posted.get("hash") == digest and posted.get("channel_id") == channel_id


async def publish(bot, meta_key: str, channel, views: List[discord.ui.LayoutView], digest: str,
                  updated: int) -> Optional[str]:
    """Edits the posted messages to `views` (posting, re-posting or deleting
    as needed) and remembers them. Returns a problem to show, or None."""
    posted = await asyncio.to_thread(state, meta_key)
    old_ids = posted.get("message_ids", [])
    if posted.get("channel_id") != channel.id:
        await _delete(bot, posted.get("channel_id"), old_ids)
        old_ids = []
    try:
        ids = await _edit_or_send(bot, channel, views, old_ids)
    except discord.NotFound:  # someone deleted one of them: post them again, in order
        await _delete(bot, channel.id, old_ids)
        ids = await _edit_or_send(bot, channel, views, [])
    except discord.HTTPException as exc:
        log.warning("Couldn't post in #%s: %s", channel.name, exc.text or exc.status)
        return f"Discord refused the message; check the bot's permissions in #{channel.name}."
    await asyncio.to_thread(db.set_meta, meta_key, json.dumps(
        {"channel_id": channel.id, "message_ids": ids, "hash": digest, "updated": updated}))
    return None


async def _edit_or_send(bot, channel, views, old_ids) -> List[int]:
    ids = []
    for i, view in enumerate(views):
        if i < len(old_ids):
            await channel.get_partial_message(old_ids[i]).edit(view=view, allowed_mentions=NO_PINGS)
            ids.append(old_ids[i])
        else:
            ids.append((await channel.send(view=view, allowed_mentions=NO_PINGS)).id)
    await _delete(bot, channel.id, old_ids[len(views):])
    return ids


async def _delete(bot, channel_id, message_ids):
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    for message_id in message_ids if channel else []:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass
