"""
The application's review card in Discord: a stored application record
(applications.py) as a Components V2 layout. Plain functions, so the card
can be built (and tested) without the cog; cogs/apply.py wires the buttons.
/lookup shows a character with the same parts (lookup_view), minus what
belongs to an application.

Layout, top to bottom:
  header      the character's portrait (Blizzard, or the applicant's Discord
              avatar), name, class/level/guild and a status line
  facts       applicant, main spec and role, offspec, item level, talents
  attention   everything worth a reviewer's look in one list: earlier
              denials, the guild's minimums, empty slots, missing enchants,
              empty sockets, another ruleset
  stats       the statistics that matter for the role, and professions
  gear        item level first, linked names, enchants and gems below
  parses      WarcraftLogs blocks, once WarcraftLogs covers the game
  note        the applicant's note as a quote, screenshots as a gallery
  decision    who decided what and what the bot did, or how to decide
  buttons     only what fits the status, plus "Open on dashboard"

The accent colour shows the status: the class colour while pending, green
when approved, red when denied. Applications from before Blizzard's data
(no "gear") show their stored text blocks instead.

Discord allows 4000 characters of text per message; the gear list gets
what the rest of the card leaves (gear_lines with a limit).
"""

import time
from typing import Callable, Dict, List, Optional, Tuple

import discord

import applications
import config
import gear
import icons

CARD_TEXT_LIMIT = 3900   # Discord's 4000, with a little room to spare
MIN_GEAR_TEXT = 600      # the gear list always gets at least this much
NOTE_LIMIT = 1024
APPROVED_COLOR = 0x3BA55D
DENIED_COLOR = 0xED4245
NEUTRAL_COLOR = 0x2B2D31
STATUS_LINES = {"pending": "🟡 **Waiting for review**", "approved": "✅ **Approved**", "denied": "❌ **Denied**"}


def icon_key(kind: str, game_id) -> str:
    """Key of an emoji in a record's "icons" ({"item:123": "<:name:id>", ...})."""
    return f"{kind}:{game_id}"


def _clip(text: str, limit: int, more: str) -> str:
    """Cuts at a line break so a line isn't split."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, max(0, limit - len(more) - 2))
    return text[:cut if cut > 0 else max(0, limit - len(more) - 2)] + "\n" + more


def accent(application: dict) -> int:
    status = application.get("status", "pending")
    if status == "approved":
        return APPROVED_COLOR
    if status == "denied":
        return DENIED_COLOR
    return application.get("class_color") or NEUTRAL_COLOR


# --- the parts ------------------------------------------------------------------------------------

def header(application: dict, guild=None, *, status: bool = True) -> str:
    summary = application.get("gear") or {}
    name = application.get("character_name") or "?"
    url = application.get("profile_url")
    lines = [f"## [{name}]({url})" if url else f"## {name}"]
    class_name = application.get("class_name") or summary.get("class_name") or ""
    class_icon = icons.resolve_class_icon(guild, class_name) if class_name else ""
    who = " ".join(filter(None, [f"Level {summary['level']}" if summary.get("level") else None, summary.get("race"),
                                 class_name]))
    details = " · ".join(filter(None, [who, f"‹{summary['guild']}›" if summary.get("guild") else None,
                                       summary.get("ruleset")]))
    if details:
        lines.append(f"{class_icon} {details}".strip())
    if not status:
        return "\n".join(lines)
    status = application.get("status", "pending")
    decision = application.get("decision") or {}
    line = STATUS_LINES.get(status, status)
    if status != "pending" and decision.get("by_name"):
        line += f" by {decision['by_name']}" + (f" · <t:{decision['at']}:R>" if decision.get("at") else "")
    elif application.get("submitted_at"):
        line += f" · applied <t:{application['submitted_at']}:R>"
    lines.append(line)
    return "\n".join(lines)


def facts(application: dict, guild=None) -> str:
    """Applicant, specs and role, item level and talents. Older records
    without Blizzard data show their stored class block."""
    summary = application.get("gear")
    lines = [f"**Applicant** <@{application['applicant_id']}>"] if application.get("applicant_id") else []
    lines.extend(application.get("extra_facts") or [])
    if not summary and application.get("class_block"):
        return "\n".join(lines + [application["class_block"]])
    class_name = application.get("class_name") or ""
    spec, role = application.get("spec"), application.get("role")
    if spec or role:
        spec_part = f"{icons.resolve_spec_icon(class_name, spec)} {spec}".strip() if spec else "?"
        role_part = f" · {icons.resolve_role_icon(guild, role)} {role}".replace("  ", " ") if role else ""
        lines.append(f"**Main spec** {spec_part}{role_part}")
    others = application.get("other_specs") or []
    if others:
        lines.append("**Offspec** " + ", ".join(f"{icons.resolve_spec_icon(class_name, s)} {s}".strip()
                                                for s in others))
    if summary:
        talents = " / ".join(f"{t['spec']} {t['points']}" for t in summary.get("talents") or [] if t["points"])
        equipped, average = summary.get("equipped_item_level"), summary.get("average_item_level")
        bags = f" ({average} with bags)" if average and equipped and average != equipped else ""
        lines.append(f"**Item level** `{equipped or '?'}`{bags}" + (f" · **Talents** {talents}" if talents else ""))
    return "\n".join(lines)


def attention(application: dict, prior_denials: List[Tuple[int, dict]]) -> Optional[str]:
    """Everything worth a reviewer's look, in one list (None when nothing)."""
    found = []
    if len(prior_denials) >= 2:
        last_id, last = prior_denials[-1]
        when = time.strftime("%d %b %Y", time.gmtime(applications.submitted_at(last_id, last)))
        found.append(f"Denied {len(prior_denials)}× before (latest: {last.get('character_name', '?')}, {when})")
    summary = application.get("gear")
    if summary:
        problems = gear.requirement_problems(summary)
        if problems:
            found.append("Below the guild's minimum: " + ", ".join(problems))
        found.extend(applications.gear_flags(summary))
    if not found:
        return None
    return "### ⚠️ Worth a look\n" + "\n".join(f"- {f}" for f in found)


def stats_block(application: dict) -> Optional[str]:
    summary = application.get("gear") or {}
    icon_map = application.get("icons") or {}
    lines = []
    highlights = gear.stat_highlights(summary.get("stats") or {}, application.get("role"))
    if highlights:
        lines.append(" · ".join(f"`{h['text']}` {h['label']}" for h in highlights))
    trades = [p for p in summary.get("professions") or [] if p.get("primary")]
    if trades:
        lines.append("**Professions** " + " · ".join(
            f"{icon_map.get(icon_key('profession', p.get('id'))) or ''} {p['name']} {p['skill']}/{p['max']}".strip()
            for p in trades))
    return "### Stats\n" + "\n".join(lines) if lines else None


def gear_lines(application: dict, limit: int,
               more: str = "-# … the rest is on the dashboard's Applications page") -> Optional[str]:
    """The gear list within `limit` characters, or None without Blizzard data."""
    summary = application.get("gear")
    if not summary:
        return application.get("armory_block")  # applications from before Blizzard's WoW Forever data
    icon_map = application.get("icons") or {}
    checked = application.get("gear_checked_at")
    head = "### Gear\n-# From Blizzard, as of the character's last logout" + (
        f" · checked <t:{checked}:R>" if checked else "")
    rows = []
    for item in summary.get("items") or []:
        if item["slot"] in gear.COSMETIC_SLOTS:
            continue
        mark = icon_map.get(icon_key("item", item.get("item_id"))) or gear.QUALITY_MARKS.get(item.get("quality") or "",
                                                                                              "▫️")
        url = gear.item_url(item.get("item_id"))
        name = f"[{item['name']}]({url})" if url else item["name"]
        row = f"`{item.get('item_level') or '?':>3}` {mark} **{name}** · {item['slot_name']}"
        extras = ([f"✨ {e.replace('Enchanted: ', '')}" for e in item.get("enchants") or []]
                  + [f"💎 {s['gem']}" if s.get("gem") else f"◻️ empty {s.get('type') or 'socket'}"
                     for s in item.get("sockets") or []])
        if item.get("set"):
            extras.insert(0, f"🔗 {item['set']}")
        if extras:
            row += "\n-# " + " · ".join(extras)
        rows.append(row)
    body = "\n".join(rows) or "-# No gear equipped."
    return _clip(f"{head}\n{body}", limit, more)


def note_block(application: dict) -> str:
    note = (application.get("note") or "").strip()
    if not note:
        return "### Note\n-# No note."
    note = note[:NOTE_LIMIT] + ("…" if len(note) > NOTE_LIMIT else "")
    return "### Note\n" + "\n".join(f"> {line}" if line else ">" for line in note.splitlines())


def decision_block(application: dict) -> str:
    """What was decided and what the bot did; older records show their footer."""
    status = application.get("status", "pending")
    decision = application.get("decision") or {}
    if status == "pending":
        reset = application.get("decision_footer") if application.get("last_decision") else None
        hint = "-# Decide with the buttons below, or on the dashboard's Applications page."
        return f"-# {reset}\n{hint}" if reset else hint
    if not decision:
        return application.get("decision_footer") or STATUS_LINES.get(status, status)
    label = decision.get("by_name", "?") + (" on the dashboard" if decision.get("via") == "dashboard" else "")
    lines = [f"{STATUS_LINES.get(status, status)} by **{label}**"
             + (f" · <t:{decision['at']}:f>" if decision.get("at") else "")]
    details = []
    if status == "approved":
        details.append("Roles given: " + (", ".join(decision.get("roles_assigned") or []) or "none"))
        if decision.get("roles_kept"):
            details.append("already had: " + ", ".join(decision["roles_kept"]))
        details.extend(filter(None, [decision.get("nickname"), decision.get("roster")]))
    elif decision.get("reason"):
        details.append(f"Reason (reviewers only): {decision['reason']}")
    lines.extend(f"-# {d}" for d in details)
    return "\n".join(lines)


def portrait(application: dict, guild=None) -> Optional[str]:
    """The character's Blizzard portrait, else the applicant's Discord avatar."""
    url = (application.get("gear") or {}).get("avatar_url")
    if url:
        return url
    member = guild.get_member(int(application["applicant_id"])) if guild is not None else None
    return member.display_avatar.url if member is not None else None


# --- the whole card -------------------------------------------------------------------------------

Callbacks = Dict[str, Callable]  # "approve" | "deny" | "reset" | "refresh" -> button callback


def build(application: dict, *, prior_denials: List[Tuple[int, dict]], callbacks: Callbacks, guild=None,
          dashboard_url: Optional[str] = None, all_buttons: bool = False) -> discord.ui.LayoutView:
    """The card as a LayoutView. `all_buttons` adds every action button
    (for registering the persistent view, so each one is answered after a
    restart); otherwise only those that fit the status."""
    status = application.get("status", "pending")
    parts = {
        "header": header(application, guild),
        "facts": facts(application, guild),
        "attention": attention(application, prior_denials),
        "stats": stats_block(application),
        "parses": "\n\n".join(filter(None, (application.get(k) for k in ("tier_block", "boss_block",
                                                                          "hidden_block")))) or None,
        "note": note_block(application),
        "decision": decision_block(application),
    }
    room = max(MIN_GEAR_TEXT, CARD_TEXT_LIMIT - sum(len(p or "") for p in parts.values()))
    parts["gear"] = gear_lines(application, room)

    container = discord.ui.Container(accent_color=discord.Color(accent(application)))
    picture = portrait(application, guild)
    if picture:
        container.add_item(discord.ui.Section(discord.ui.TextDisplay(parts["header"]),
                                              accessory=discord.ui.Thumbnail(picture)))
    else:
        container.add_item(discord.ui.TextDisplay(parts["header"]))
    container.add_item(discord.ui.TextDisplay(parts["facts"]))
    if parts["attention"]:
        container.add_item(discord.ui.TextDisplay(parts["attention"]))
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    for key in ("stats", "gear", "parses"):
        if parts[key]:
            container.add_item(discord.ui.TextDisplay(parts[key]))
    if application.get("wcl_url"):
        container.add_item(discord.ui.TextDisplay(f"-# [WarcraftLogs profile]({application['wcl_url']})"))
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(discord.ui.TextDisplay(parts["note"]))
    screenshots = (application.get("screenshot_urls") or [])[: config.MAX_SCREENSHOTS]
    if screenshots:
        container.add_item(discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in screenshots]))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(parts["decision"]))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    row = discord.ui.ActionRow()
    pending = status == "pending"
    # The custom IDs are unchanged from the first version, so every card ever posted keeps working.
    for key, label, emoji, style, custom_id, show in (
        ("approve", "Approve", "✅", discord.ButtonStyle.success, "wow_apply_approve", pending),
        ("deny", "Deny", "✖️", discord.ButtonStyle.danger, "wow_apply_deny", pending),
        ("reset", "Reset decision", "↩️", discord.ButtonStyle.secondary, "wow_apply_reset", not pending),
        ("refresh", "Refresh", "🔃", discord.ButtonStyle.secondary, "wow_apply_refresh_gear",
         bool(application.get("character_slug"))),
    ):
        if show or all_buttons:
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=custom_id)
            button.callback = callbacks[key]
            row.add_item(button)
    if dashboard_url and not all_buttons:
        row.add_item(discord.ui.Button(label="Open on dashboard", style=discord.ButtonStyle.link, url=dashboard_url))
    view.add_item(row)
    return view


def lookup_view(summary: dict, *, icons_by_key: Optional[Dict[str, str]] = None, profile_url: Optional[str] = None,
                guild=None, dashboard_url: Optional[str] = None,
                extra_facts: Optional[List[str]] = None) -> discord.ui.LayoutView:
    """/lookup: a character (gear.fetch) drawn like an application card,
    without the applicant, note, decision and buttons. `extra_facts`: lines
    under the header (e.g. where it stands on the guild roster)."""
    record = {"character_name": summary.get("character"), "class_name": summary.get("class_name"),
              "class_color": config.CLASS_COLORS.get(summary.get("class_name") or "", NEUTRAL_COLOR),
              "gear": summary, "icons": icons_by_key or {}, "profile_url": profile_url,
              "gear_checked_at": int(time.time()), "extra_facts": extra_facts or []}
    parts = {
        "header": header(record, guild, status=False),
        "facts": facts(record, guild),
        "attention": attention(record, []),
        "stats": stats_block(record),
    }
    room = max(MIN_GEAR_TEXT, CARD_TEXT_LIMIT - sum(len(p or "") for p in parts.values()))
    parts["gear"] = gear_lines(record, room, "-# … the rest is on the dashboard's Character lookup")

    container = discord.ui.Container(accent_color=discord.Color(accent(record)))
    if summary.get("avatar_url"):
        container.add_item(discord.ui.Section(discord.ui.TextDisplay(parts["header"]),
                                              accessory=discord.ui.Thumbnail(summary["avatar_url"])))
    else:
        container.add_item(discord.ui.TextDisplay(parts["header"]))
    if parts["facts"]:
        container.add_item(discord.ui.TextDisplay(parts["facts"]))
    if parts["attention"]:
        container.add_item(discord.ui.TextDisplay(parts["attention"]))
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    for key in ("stats", "gear"):
        if parts[key]:
            container.add_item(discord.ui.TextDisplay(parts[key]))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    if dashboard_url:
        view.add_item(discord.ui.ActionRow(discord.ui.Button(label="Open in Character lookup",
                                                             style=discord.ButtonStyle.link, url=dashboard_url)))
    return view
