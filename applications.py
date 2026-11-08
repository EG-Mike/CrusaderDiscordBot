"""
Applications: the records behind the review cards, shared by cogs/apply.py
(Discord: the apply flow, the review card and its buttons) and
webapi/applications_api.py (the dashboard's Applications page).

Records live in the application store (bot.store, storage.ApplicationStore),
keyed by the review card's Discord message ID, which is also the
application's ID on the dashboard. Archiving re-posts a card in the archive
channel, so the record moves to the new message's ID; it keeps
`original_message_id`, so old dashboard links still find it.

Rules that change a record (approve, deny, reset, refresh gear, archive)
are methods on the cog, since they need Discord (roles, DMs, the card).
This module holds what doesn't: finding records, how a record looks to the
dashboard, and telling the dashboard something changed.

Fields of a record (older records may lack the newer ones):
  applicant_id, character_name, character_slug, status (pending | approved |
  denied), class_name, class_color, role, spec, spec_id, other_specs
  (the offspec, as a list of one), off_spec_id, note, screenshot_urls,
  gear (gear.summarise), gear_text, gear_checked_at, profile_url, wcl_url,
  channel_id, submitted_at, original_message_id, last_decision,
  decision ({status, by_id, by_name, via, at, roles_assigned, roles_kept,
  nickname, roster, reason}), decision_footer (the card's footer text).
"""

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import config
import gear

log = logging.getLogger("wow-apply-bot.applications")

STATUSES = ("pending", "approved", "denied")
DISCORD_EPOCH_MS = 1420070400000


class ApplicationError(Exception):
    """An application can't be changed this way; the message says why."""


@dataclass
class Reviewer:
    """Who decided: a Discord user, on Discord or on the dashboard."""
    id: int
    name: str
    via: str = "discord"  # "discord" | "dashboard"

    @property
    def label(self) -> str:
        return f"{self.name} (dashboard)" if self.via == "dashboard" else self.name


# --- finding records ------------------------------------------------------------------------------

def is_application(record) -> bool:
    return isinstance(record, dict) and "applicant_id" in record and "character_name" in record


def records(store) -> List[Tuple[int, dict]]:
    """Every application as (message id, record), newest first. Other keys
    in the store (e.g. the archive confirmation) are skipped."""
    found = []
    for key, record in store.items():
        if is_application(record) and str(key).isdigit():
            found.append((int(key), record))
    found.sort(key=lambda pair: submitted_at(*pair), reverse=True)
    return found


def find(store, application_id) -> Tuple[int, dict]:
    """(message id, record) by ID, also by the ID it had before archiving."""
    try:
        wanted = int(application_id)
    except (TypeError, ValueError):
        raise ApplicationError("Unknown application.") from None
    record = store.get(wanted)
    if is_application(record):
        return wanted, record
    for message_id, record in records(store):
        if record.get("original_message_id") == wanted:
            return message_id, record
    raise ApplicationError("That application doesn't exist (any more).")


def snowflake_time(snowflake: int) -> int:
    """Unix seconds a Discord ID was made at."""
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) // 1000


def submitted_at(message_id: int, record: dict) -> int:
    return int(record.get("submitted_at") or snowflake_time(record.get("original_message_id") or message_id))


def prior_denials(store, applicant_id: int, exclude: Optional[int] = None) -> List[Tuple[int, dict]]:
    """The applicant's other denied applications, oldest first."""
    return sorted(((mid, rec) for mid, rec in records(store)
                   if rec.get("applicant_id") == applicant_id and rec.get("status") == "denied" and mid != exclude),
                  key=lambda pair: submitted_at(*pair))


def offspec(record: dict) -> Optional[str]:
    others = record.get("other_specs") or []
    return others[0] if others else None


# --- how the dashboard sees a record --------------------------------------------------------------

def gear_flags(summary: Optional[dict]) -> List[str]:
    """The gear check's warnings, in words (same as the review card's)."""
    if not summary:
        return []
    found = []
    if gear.off_ruleset(summary):
        found.append(f"On the {summary.get('ruleset') or 'another'} ruleset, not one of the guild's")
    if summary.get("empty_slots"):
        found.append("Empty: " + ", ".join(summary["empty_slots"]))
    if summary.get("missing_enchants"):
        found.append("No enchant: " + ", ".join(summary["missing_enchants"]))
    if summary.get("empty_sockets"):
        n = summary["empty_sockets"]
        found.append(f"{n} empty gem socket{'s' if n != 1 else ''}")
    return found


def _member(guild, user_id: int) -> Optional[dict]:
    member = guild.get_member(int(user_id)) if guild is not None and user_id else None
    if member is None:
        return None
    return {"id": str(member.id), "name": member.display_name, "username": member.name,
            "avatar_url": member.display_avatar.url}


def message_url(guild, record: dict, message_id: int) -> Optional[str]:
    channel_id = record.get("channel_id") or config.REVIEW_CHANNEL_ID
    if guild is None or not channel_id:
        return None
    return f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"


def public_row(store, guild, message_id: int, record: dict) -> dict:
    """What the Applications list shows. IDs are strings (JavaScript)."""
    summary = record.get("gear") or {}
    decision = record.get("decision") or {}
    return {
        "id": str(message_id),
        "status": record.get("status", "pending"),
        "character": record.get("character_name"),
        "character_slug": record.get("character_slug"),
        "class_name": record.get("class_name"),
        "level": summary.get("level"),
        "item_level": summary.get("equipped_item_level"),
        "guild": summary.get("guild"),
        "role": record.get("role"),
        "spec": record.get("spec"),
        "offspec": offspec(record),
        "applicant": _member(guild, record.get("applicant_id")) or {"id": str(record.get("applicant_id")),
                                                                     "name": None, "username": None,
                                                                     "avatar_url": None},
        "applicant_in_server": _member(guild, record.get("applicant_id")) is not None,
        "submitted_at": submitted_at(message_id, record),
        "has_gear": bool(summary),
        "flag_count": len(gear_flags(summary)) + len(gear.requirement_problems(summary) if summary else []),
        "has_note": bool(record.get("note")),
        "screenshot_count": len(record.get("screenshot_urls") or []),
        "prior_denials": len(prior_denials(store, record.get("applicant_id"), exclude=message_id)),
        "archived": bool(record.get("original_message_id")),
        "decided_by": decision.get("by_name"),
        "decided_at": decision.get("at"),
    }


def public_detail(store, guild, message_id: int, record: dict, *,
                  icon_urls: Optional[Dict[int, Optional[str]]] = None,
                  screenshot_urls: Optional[List[str]] = None) -> dict:
    """Everything the application's page shows: the list row plus the gear
    (with icon images), statistics, note, screenshots and the decision.
    Moderator-only: the note and the reviewer's reason are private text."""
    summary = record.get("gear")
    icon_urls = icon_urls or {}
    gear_data = None
    if summary:
        gear_data = {**summary,
                     "items": [{**i, "icon_url": icon_urls.get(i.get("item_id"))} for i in summary.get("items", [])],
                     "stat_highlights": gear.stat_highlights(summary.get("stats") or {}, record.get("role")),
                     "stat_groups": gear.stat_groups(summary.get("stats") or {})}
    decision = dict(record.get("decision") or {})
    if decision.get("by_id"):
        decision["by_id"] = str(decision["by_id"])
    return {
        **public_row(store, guild, message_id, record),
        "note": record.get("note"),
        "screenshots": screenshot_urls if screenshot_urls is not None else list(record.get("screenshot_urls") or []),
        "gear": gear_data,
        "gear_checked_at": record.get("gear_checked_at"),
        "flags": gear_flags(summary),
        "requirement_problems": gear.requirement_problems(summary) if summary else [],
        "profile_url": record.get("profile_url"),
        "wcl_url": record.get("wcl_url"),
        "message_url": message_url(guild, record, message_id),
        "decision": decision or None,
        "decision_text": record.get("decision_footer"),  # older records only have the card's footer
        "legacy_text": None if summary else "\n\n".join(filter(None, (
            record.get("class_block"), record.get("armory_block"), record.get("tier_block"),
            record.get("boss_block")))) or None,
        # Parses join here once WarcraftLogs covers the game version (APPLICATION_WARCRAFTLOGS_ENABLED).
        "warcraftlogs": {"enabled": bool(config.APPLICATION_WARCRAFTLOGS_ENABLED),
                         "text": "\n\n".join(filter(None, (record.get("tier_block"), record.get("boss_block"))))
                         or None},
    }


# --- telling the dashboard ------------------------------------------------------------------------

_listeners: Dict[str, Callable[[str], None]] = {}


def on_change(name: str, callback: Callable[[str], None]):
    """Calls callback(application id) after any change. One callback per
    name (registering again replaces it)."""
    _listeners[name] = callback


def changed(message_id):
    for name, callback in list(_listeners.items()):
        try:
            callback(str(message_id))
        except Exception:
            log.exception("Application change listener %s failed", name)


def discord_reviewer(user) -> Reviewer:
    return Reviewer(id=user.id, name=str(user), via="discord")
