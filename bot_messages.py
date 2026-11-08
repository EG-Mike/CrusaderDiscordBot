"""
The bot's standard messages - the pinned explainers and the DMs it sends -
as editable texts.

Each message is a Definition below: where it appears, its built-in default
text, and the {placeholders} it can use. Moderators edit them on the
dashboard's Bot messages page; saved versions live in the database (table
`bot_messages`, with every saved version in `bot_message_history`), and a
message without a saved version uses its default.

Cogs never hardcode these texts: they call `embed(key, ...)` or
`text(key, ...)`, passing values for placeholders that depend on the
moment (e.g. the applicant's {character}); placeholders that come from
settings are filled in here.

Pinned explainers are kept in sync with their text: the cog that owns one
registers an updater (`register_updater`) that edits the pinned message, or
posts it if it's missing. It runs at startup and when a moderator clicks
"Update in Discord".

Placeholders are replaced by plain text substitution of known names only
(never str.format), so text typed in the dashboard can't reach anything
else in the bot.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import discord

import config
import db

DEFAULT_COLOR = "#5865F2"  # Discord blurple
MAX_EMBED_TITLE = 256
MAX_EMBED_TEXT = 4096
MAX_MESSAGE_TEXT = 2000

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class BotMessageError(Exception):
    """Something the person can fix; the message says what."""


@dataclass(frozen=True)
class Variable:
    name: str
    description: str
    sample: str = ""
    # Filled in from settings when set; otherwise the caller passes it.
    source: Optional[Callable[[], object]] = None


@dataclass(frozen=True)
class Definition:
    key: str
    label: str
    group: str
    description: str
    kind: str                       # "embed" (pinned explainer) | "text" (plain message, e.g. a DM)
    where: str
    default_body: str
    default_title: Optional[str] = None
    default_color: Optional[str] = None
    variables: List[Variable] = field(default_factory=list)
    marker: Optional[str] = None    # embed footer that identifies the pinned message
    old_markers: Tuple[str, ...] = ()  # earlier markers: a message pinned with one is updated, not posted again
    feature: Optional[str] = None   # FEATURE_* flag the message depends on


# --- built-in default texts ----------------------------------------------------------

_APPLY_EXPLAINER = (
    "Click the blue button below this message to apply.\n"
    "\n"
    ":one:. Type your character's name and surname (e.g. Eggmike Thewhite). Log out once in your raid gear "
    "first: the bot reads your character from Blizzard as of your last logout.\n"
    ":two:. The bot looks up your gear, enchants, talents and stats, and DMs you: your main spec is "
    "picked from your talents (you can change it), and you can add an offspec.\n"
    ":three:. Optionally, reply with a note and/or screenshots - or press Skip. Without a reply, your "
    "application is posted automatically after 3 minutes.\n"
    ":four:. You can add more information at any time by typing `/update-application` in the bot's DM.\n"
    ":five:. Our recruitment team reviews your application and you'll get a DM with the outcome.\n"
    "\n"
    "When approved, you will be granted the correct roles and further instructions on how to sign up "
    "for the raid(s)"
)

_REVIEW_EXPLAINER = (
    "Here's the full flow, walked through with an example applicant, **Skullcrusher**:\n"
    "\n"
    "**1. Before a card ever shows up here**\n"
    "Skullcrusher clicks the button in the apply channel, gives their character's name and surname, and "
    "the bot reads their character from Blizzard: level, guild, talents, stats, and every equipped item "
    "with its item level, enchants and gems. Empty slots, missing enchants and empty sockets are flagged, "
    "and so is anything below the guild's minimum level or item level. Over DM, they confirm their main "
    "spec and role (suggested from their talents), can pick an offspec, and can add an optional note or "
    "screenshot(s). Once that's done (or skipped, or it times out), a card posts right here - and on the "
    "dashboard's Applications page. That's the point where it becomes your job.\n"
    "Blizzard updates characters when a player logs out: 🔃 **Refresh** reads it again, e.g. "
    "after they levelled up or changed gear.\n"
    "\n"
    "**2. Clicking {approve_emoji} Approve** (here or on the dashboard)\n"
    "- They're assigned the Fresh role - unless they already hold Fresh, Regular, or Organizer, in "
    "which case that's skipped and the card says so instead of pretending something changed.\n"
    "- They're also auto-tagged with their class (e.g. Mage) and role (Tank/Healer/DPS) roles.\n"
    "- Their character is linked to them on the guild roster, with their main spec and offspec "
    "(unless someone else already linked it). Below level 10 Blizzard doesn't show the character yet: "
    "the link is then made as soon as it shows up, e.g. when it joins the guild.\n"
    "- Their server nickname is changed to their character name - again skipped for "
    "already-established members.\n"
    "- The card's footer shows exactly what was newly assigned vs. what they already had, plus the "
    "nickname change (or why it didn't happen).\n"
    "- Skullcrusher gets a DM confirming they're in.\n"
    "\n"
    "**3. Clicking {deny_emoji} Deny**\n"
    "- You're asked for an optional reason. It's saved for other reviewers to see - it's "
    "**never** shown to the applicant.\n"
    "- Skullcrusher gets a DM letting them know it didn't work out **only on a first-time denial**. "
    "If you're instead correcting an earlier approval into a denial, no DM is sent - their Fresh "
    "role is just quietly removed.\n"
    "\n"
    "**4. ↩️ Reset decision**\n"
    "Made the wrong call? Reset puts the card back to pending - Approve/Deny become clickable again. "
    "Re-deciding still follows the rules above: approving after a wrong denial sends the acceptance "
    "DM as normal; denying after a wrong approval doesn't send a denial DM, since they were never "
    "told they were out.\n"
    "\n"
    "**5. `/applications archive`** (or Archive on the dashboard)\n"
    "Moves every decided card (approved or denied) out of this channel into the archive channel, "
    "keeping this one focused on what's still pending. A single confirmation message here tracks who "
    "last ran it and when - it gets updated in place, not reposted, each time.\n"
    "\n"
    "**6. `/lookup`**\n"
    "Look anyone up without an application: the same character summary, shown only to you unless you "
    "choose to post it. The dashboard's Character lookup does the same."
)

_ATTENDANCE_EXPLAINER = (
    "**The short version:** \n"
    "After each main-raid manually add the log to the log list (no efficient way to do this "
    "automatically, too many variables)\n"
    "This will then be used to calculate attendance. \n"
    "to be Regular-eligible, someone needs to have attended (killed at least `{min_kills}` boss) in "
    "at least `{min_attended}` of the last `{window}` tagged main raids.\n"
    "\n"
    "When people join a raid with their alts (and we want that to count towards attendance, you "
    "should add their character as an alt by using '/checkattendance link')**📋 Main Raid Log List** "
    "- the list of raids that actually count. Only logs added here are used - nothing is "
    "auto-detected from WarcraftLogs, so a random alt run or an off-night log never sneaks in. Use "
    "the ➕ Add Log / ➖ Remove Log buttons (Remove asks for the #ID shown next to each entry), or "
    "`/checkattendance addlog` / `removelog id:`.\n"
    "\n"
    "**🧾 Raider Roster** - shows every Regular member, plus Fresh members who've actually shown up "
    "in at least one of the last `{fresh_window}` logs (keeps this readable even with a large Fresh "
    "population). Main character and any alts shown with class icons. If someone's Discord nickname "
    "doesn't match their character name, fix it with `/checkattendance setmain`. Alts are added with "
    "`/checkattendance link`\n"
    "\n"
    ". The 🚫 Excused section at the bottom lists anyone sitting out attendance tracking (vacation, "
    "break, etc.) - **+ Add player** asks for a name and reason; **- Remove player** asks for the "
    "#ID next to their entry. `/checkattendance exclude name: reason:` offers real live name "
    "suggestions as you type, if you'd rather use the command; removal by command is "
    "`/checkattendance removeexcluded id:`.\n"
    "\n"
    "**📊 Attendance Overview** - the actual promotion/demotion discussion aid. Click Refresh (or run "
    "`/checkattendance run`, which has no cooldown(DON'T ABUSE)) to recompute it against the current "
    "log list.\n"
    "\n"
    "Every button here has a matching slash command too, if you'd rather type than click - "
    "`/checkattendance` to see them all."
)

_NOTE_PROMPT_DM = (
    "**This step is optional:**\n"
    "\n"
    "You can provide a note and/or screenshot(s) by simply replying to this chat.\n"
    "This information will be added added to your application and can be seen by the reviewers\n"
    "\n"
    "Your application will automatically post in 3 minutes. You may press the skip button to "
    "immediately continue\n"
    "\n"
    "**At any time during the review, you can reply with `/update-application` to be able to "
    "add/change your note/screenshots**"
)

_NOTE_TIMEOUT_DM = (
    "⏱️ Time's up - I didn't receive a note or screenshot in time, but no worries: your application "
    "has still been submitted to the reviewers. You can still add a note or screenshot(s) later with "
    "`/update-application`."
)

_APPROVAL_DM = (
    "🎉 Your character **{character}** has been approved! Welcome to Crusader's PUG.\n"
    "\n"
    "You have been granted access to the raid-signup channel(s).\n"
    "Make sure to sign-up and confirm your spot when you are placed on the roster!\n"
    "\n"
    "One more step: When you sign up for a raid, you will need to register your character for raid "
    "sign-ups - this is a one time process!Please note: your (server-specific) Discord nickname has "
    "been changed to your characters name."
)

_DENIAL_DM = (
    "Thanks for applying, **{character}**. Unfortunately we won't be moving forward with your "
    "application at this time. Feel free to reach out to an officer if you have questions or come "
    "back later."
)


_CHARACTER = Variable("character", "The applicant's character name", "Skullcrusher")

DEFINITIONS: List[Definition] = [
    Definition(
        key="gear_check_explainer",  # key kept from the gear-check days, so saved texts stay
        label="Application explainer",
        group="Applications",
        description="Explains how to apply. Carries the button that starts an application.",
        kind="embed",
        where="Pinned in the apply channel",
        default_title="How to apply and get the permissions to sign up for raids",
        default_body=_APPLY_EXPLAINER,
        marker="application-explainer",
        old_markers=("gear-check-explainer",),
    ),
    Definition(
        key="review_explainer",
        label="Review channel explainer",
        group="Applications",
        description="Explains the review process to whoever approves or denies applications.",
        kind="embed",
        where="Pinned in the application review channel",
        default_title="How the application review process works",
        default_body=_REVIEW_EXPLAINER,
        variables=[
            Variable("approve_emoji", "The Approve button's emoji", "✅", lambda: "✅"),
            Variable("deny_emoji", "The Deny button's emoji", "✖️", lambda: "✖️"),
        ],
        marker="review-channel-explainer",
    ),
    Definition(
        key="note_prompt_dm",
        label="Note and screenshot request",
        group="Applications",
        description="Sent while applying, asking for an optional note or screenshots.",
        kind="text",
        where="DM to the applicant",
        default_body=_NOTE_PROMPT_DM,
    ),
    Definition(
        key="note_timeout_dm",
        label="Note request timed out",
        group="Applications",
        description="Sent when the applicant didn't send a note or screenshot in time.",
        kind="text",
        where="DM to the applicant",
        default_body=_NOTE_TIMEOUT_DM,
    ),
    Definition(
        key="approval_dm",
        label="Application approved",
        group="Applications",
        description="Sent when an application is approved (not when re-approving after a reset).",
        kind="text",
        where="DM to the applicant",
        default_body=_APPROVAL_DM,
        variables=[_CHARACTER],
    ),
    Definition(
        key="denial_dm",
        label="Application denied",
        group="Applications",
        description="Sent on a first denial only. The reviewer's reason is never included.",
        kind="text",
        where="DM to the applicant",
        default_body=_DENIAL_DM,
        variables=[_CHARACTER],
    ),
    Definition(
        key="attendance_explainer",
        label="Attendance explainer",
        group="Attendance",
        description="Explains the attendance messages and commands.",
        kind="embed",
        where="Pinned in the attendance channel",
        default_title="How attendance tracking works",
        default_body=_ATTENDANCE_EXPLAINER,
        variables=[
            Variable("min_kills", "Boss kills needed for a raid to count as attended",
                     source=lambda: config.ATTENDANCE_MIN_KILLS_PER_LOG),
            Variable("min_attended", "Raids someone must attend to be Regular-eligible",
                     source=lambda: config.ATTENDANCE_MIN_ATTENDED),
            Variable("window", "How many recent main raids are counted", source=lambda: config.ATTENDANCE_WINDOW),
            Variable("fresh_window", "Recent logs a Fresh member must appear in to be on the roster",
                     source=lambda: config.ROSTER_FRESH_ACTIVITY_WINDOW),
        ],
        marker="attendance-explainer",
        feature="FEATURE_ATTENDANCE_ENABLED",
    ),
]
DEFINITIONS.append(Definition(
    key="guild_roster_header",
    label="Guild roster header",
    group="Guild roster",
    description="The heading of the guild roster the bot keeps posted (Settings, Guild roster: Roster channel).",
    kind="text",
    where="At the top of the posted guild roster",
    default_body="## {guild}\n-# {count} characters · levels update when a player logs out · updated {updated}",
    variables=[
        Variable("guild", "The guild's name in the game", "Encore"),
        Variable("count", "How many characters are listed", "34"),
        Variable("updated", "When the roster last changed (shown as e.g. \"5 minutes ago\")", "5 minutes ago"),
    ],
    feature="FEATURE_ROSTER_ENABLED",
))
DEFINITIONS.append(Definition(
    key="level_milestone",
    label="Level milestone",
    group="Leveling",
    description="Posted when a character reaches one of the level milestones (Settings, Leveling).",
    kind="text",
    where="Posted in the leveling channel",
    default_body="🎉 Congratulations {member}! **{character}** just reached level **{level}**!",
    variables=[
        Variable("member", "The member (a mention), or the character's first name when nobody has linked it yet",
                 "@Eggmike"),
        Variable("character", "The character's name and surname", "Eggmike Thewhite"),
        Variable("level", "The character's new level", "20"),
        Variable("class", "The character's class", "Priest"),
        Variable("milestone", "The milestone reached (the same as the level, unless several were passed at once)",
                 "20"),
    ],
    feature="FEATURE_LEVELING_ENABLED",
))
BY_KEY: Dict[str, Definition] = {d.key: d for d in DEFINITIONS}


def definition(key: str) -> Definition:
    if key not in BY_KEY:
        raise BotMessageError("Unknown message.")
    return BY_KEY[key]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- reading -------------------------------------------------------------------------------

def _saved(key: str) -> Optional[dict]:
    rows = db.query("SELECT title, body, color, updated_at, updated_by FROM bot_messages WHERE key = ?", (key,))
    return dict(rows[0]) if rows else None


def current(key: str) -> dict:
    """The text in use: the saved version, or the default."""
    d = definition(key)
    saved = _saved(key)
    if saved:
        return {**saved, "customized": True}
    return {"title": d.default_title, "body": d.default_body, "color": d.default_color,
            "updated_at": None, "updated_by": None, "customized": False}


def history(key: str, limit: int = 30) -> List[dict]:
    definition(key)
    rows = db.query("SELECT id, title, body, color, saved_at, saved_by FROM bot_message_history "
                    "WHERE key = ? ORDER BY id DESC LIMIT ?", (key, limit))
    return [dict(r) for r in rows]


def variable_values(key: str) -> Dict[str, str]:
    """Values for placeholders that come from settings."""
    values = {}
    for variable in definition(key).variables:
        if variable.source is not None:
            try:
                values[variable.name] = str(variable.source())
            except Exception:
                values[variable.name] = variable.sample
    return values


def sample_values(key: str) -> Dict[str, str]:
    """Values for a preview: settings where available, samples otherwise."""
    return {**{v.name: v.sample for v in definition(key).variables}, **variable_values(key)}


def fill(text: Optional[str], values: Dict[str, object]) -> Optional[str]:
    """Replaces {name} for known names; anything else is left as typed."""
    if not text:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def unknown_placeholders(key: str, *texts: Optional[str]) -> List[str]:
    known = {v.name for v in definition(key).variables}
    found = []
    for text in texts:
        for name in _PLACEHOLDER_RE.findall(text or ""):
            if name not in known and name not in found:
                found.append(name)
    return found


def render(key: str, **values) -> dict:
    """{title, body, color} with placeholders filled in."""
    msg = current(key)
    all_values = {**variable_values(key), **values}
    return {"title": fill(msg["title"], all_values), "body": fill(msg["body"], all_values),
            "color": msg["color"] or DEFAULT_COLOR}


def embed(key: str, **values) -> discord.Embed:
    msg = render(key, **values)
    result = discord.Embed(title=msg["title"] or None, description=msg["body"],
                           color=discord.Color(int(msg["color"][1:], 16)))
    marker = definition(key).marker
    if marker:
        result.set_footer(text=marker)
    return result


def text(key: str, **values) -> str:
    return render(key, **values)["body"]


def embed_matches(existing: discord.Embed, wanted: discord.Embed) -> bool:
    return (existing.title == wanted.title and existing.description == wanted.description
            and (existing.color.value if existing.color else None) == (wanted.color.value if wanted.color else None)
            and existing.footer.text == wanted.footer.text)


def _button_labels(components) -> List[str]:
    """The labels of a message's (or a view's) buttons, in order."""
    labels = []
    for item in components or []:
        children = getattr(item, "children", None)
        if children is not None:
            labels.extend(_button_labels(children))
        elif getattr(item, "label", None) is not None:
            labels.append(item.label)
    return labels


# --- writing ----------------------------------------------------------------------------------

def validate(key: str, title: Optional[str], body: str, color: Optional[str]) -> dict:
    d = definition(key)
    body = (body or "").replace("\r\n", "\n").strip("\n")
    if not body.strip():
        raise BotMessageError("The message can't be empty. Use Reset to go back to the default text.")
    title = (title or "").strip() or None if d.kind == "embed" else None
    color = (color or "").strip().upper() or None if d.kind == "embed" else None
    if color and not _COLOR_RE.match(color):
        raise BotMessageError("The colour must look like #5865F2.")
    sample = sample_values(key)
    if title and len(fill(title, sample)) > MAX_EMBED_TITLE:
        raise BotMessageError(f"The title can be at most {MAX_EMBED_TITLE} characters.")
    limit = MAX_EMBED_TEXT if d.kind == "embed" else MAX_MESSAGE_TEXT
    length = len(fill(body, sample))
    if length > limit - 50:  # room for longer real values than the samples
        raise BotMessageError(f"The text is {length} characters; Discord allows {limit} here (keep it under {limit - 50}).")
    return {"title": title, "body": body, "color": color}


def save(key: str, title: Optional[str], body: str, color: Optional[str], by: str) -> dict:
    clean = validate(key, title, body, color)
    d = definition(key)
    if (clean["title"], clean["body"], clean["color"]) == (d.default_title, d.default_body, d.default_color):
        return reset(key, by)
    stamp = _now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO bot_messages (key, title, body, color, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET title = excluded.title, body = excluded.body, color = excluded.color, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, clean["title"], clean["body"], clean["color"], stamp, by),
        )
        conn.execute("INSERT INTO bot_message_history (key, title, body, color, saved_at, saved_by) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (key, clean["title"], clean["body"], clean["color"], stamp, by))
    return current(key)


def reset(key: str, by: str) -> dict:
    d = definition(key)
    if _saved(key) is not None:
        with db.transaction() as conn:
            conn.execute("DELETE FROM bot_messages WHERE key = ?", (key,))
            conn.execute("INSERT INTO bot_message_history (key, title, body, color, saved_at, saved_by) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (key, d.default_title, d.default_body, d.default_color,
                                                       _now(), f"{by} (reset to default)"))
    return current(key)


# --- keeping pinned messages in sync ------------------------------------------------------------

Updater = Callable[[], Awaitable[str]]
_UPDATERS: Dict[str, Updater] = {}


def register_updater(key: str, updater: Updater):
    """Called by the cog that owns a pinned message: `updater()` edits it to
    the current text (or posts it) and returns what it did, in words."""
    definition(key)
    _UPDATERS[key] = updater


def can_update(key: str) -> bool:
    return key in _UPDATERS


async def update_in_discord(key: str) -> str:
    d = definition(key)
    if d.kind != "embed":
        raise BotMessageError("This message is sent when needed, so there's nothing to update. "
                              "The new text is used from the next time it's sent.")
    if key not in _UPDATERS:
        if d.feature and not getattr(config, d.feature, True):
            raise BotMessageError("Its feature is switched off, so the bot isn't managing this message right now.")
        raise BotMessageError("The bot isn't managing this message right now (is it starting up?). "
                              "It will be updated at the next start.")
    return await _UPDATERS[key]()


async def _pinned_messages(channel) -> list:
    """Newer discord.py pages through pins with `async for`; older versions
    return a list when awaited (now deprecated)."""
    pins = channel.pins()
    if hasattr(pins, "__aiter__"):
        return [message async for message in pins]
    return await pins


async def sync_pinned(channel, key: str, *, view=None, on_posted=None) -> str:
    """Shared updater logic: finds the pinned message by its footer marker
    and edits it if its text changed, or posts and pins it if it's missing.
    Raises BotMessageError with a reason when the channel is unusable."""
    wanted = embed(key)
    marker = definition(key).marker
    if channel is None:
        raise BotMessageError("Its channel isn't set, or the bot can't see it. Check Settings.")
    try:
        pins = await _pinned_messages(channel)
    except discord.Forbidden:
        raise BotMessageError(f"The bot can't read the pinned messages in #{channel.name}.") from None
    markers = {marker, *definition(key).old_markers}
    existing = next((m for m in pins if m.embeds and m.embeds[0].footer.text in markers), None)
    if existing is not None:
        same_buttons = view is None or _button_labels(existing.components) == _button_labels(view.children)
        if embed_matches(existing.embeds[0], wanted) and same_buttons:
            return f"Already up to date in #{channel.name}."
        try:
            await (existing.edit(embed=wanted, view=view) if view is not None else existing.edit(embed=wanted))
        except discord.HTTPException as exc:
            raise BotMessageError(f"Discord refused the edit: {exc.text or exc.status}") from None
        return f"Updated the pinned message in #{channel.name}."
    try:
        message = await channel.send(embed=wanted, view=view) if view is not None else await channel.send(embed=wanted)
    except discord.HTTPException as exc:
        raise BotMessageError(f"Couldn't post in #{channel.name}: {exc.text or exc.status}") from None
    if on_posted is not None:
        on_posted(message)
    try:
        await message.pin()
    except discord.HTTPException:
        return f"Posted in #{channel.name}, but couldn't pin it (the bot needs Manage Messages)."
    return f"Posted and pinned in #{channel.name}."
