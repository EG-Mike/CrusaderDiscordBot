"""
Polls feature - general-purpose polls, optionally with member-added options.

`/poll create` posts one self-updating poll message. The poll starter picks
how it behaves via the command's options (each defaults from the POLL_*
constants in config/deployment.py):

  options           Fixed options to start with ("Yes; No; Maybe"). Leave
                    empty and members add their own (e.g. a guild-name vote).
  members_can_add   Whether members may add options themselves.
  max_adds          How many options each member may add.
  collect_first     With member-added options: True = an "adding" phase
                    first, voting opens later (fair for the late adders);
                    False = adding and voting run side by side.
  max_votes         How many options each member may pick (1 = classic,
                    more = approval-style "pick your favourites").
  live_results      Show the tally while voting, or only once closed.
  show_authors      Show who added each option.
  show_voters       Make votes public - who voted for what.
  allow_vote_change Whether a member can change / withdraw their vote.
  duration          Auto-close voting after e.g. "2d", "12h", "1d6h". The
                    clock starts when voting opens.

Phases: ADDING (only when collect_first and members can add) -> VOTING ->
CLOSED. The poll starter and moderators control it from the "⚙️ Manage"
button: open/close/reopen, add or remove options, peek at hidden results,
and adjust settings. Privacy settings (show_authors/show_voters) can only be
switched OFF after posting, never on - people added/voted under the
promise of anonymity.

On close the bot replies to the poll with the winner. Every poll message
ends with config.POLL_FOOTER.

Button/select custom_ids are static and the poll is looked up from
interaction.message.id (same pattern as cogs/announcements.py), so one
dummy view registered in cog_load keeps every poll alive across restarts.
Records live in polls.json via the shared storage.py store.
"""

import os
import re
import time
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.polls")

# Discord caps a select menu at 25 options - voting is one select, so that's
# the hard ceiling on how many options one poll can have.
MAX_OPTIONS = 25
OPTION_MAX_LENGTH = 55
QUESTION_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 400
MAX_DURATION_SECONDS = 30 * 24 * 3600
# Message text budget - Discord caps a Components V2 message's total text at
# 4000 chars; the option list gets whatever the header/footer leave over.
TEXT_BUDGET = 3800
VOTERS_SHOWN_INLINE = 8

PHASE_ADDING = "adding"
PHASE_VOTING = "voting"
PHASE_CLOSED = "closed"

ADD_ID = "poll_add_option"
WITHDRAW_ID = "poll_withdraw"
VOTE_SELECT_ID = "poll_vote_select"
MY_VOTES_ID = "poll_my_votes"
CLEAR_VOTES_ID = "poll_clear_votes"
VOTERS_ID = "poll_voters"
MANAGE_ID = "poll_manage"

NO_PINGS = discord.AllowedMentions.none()

# Manage-panel toggles: record key -> (label, one_way_off). one_way_off
# settings are privacy promises and can only be turned off after posting.
TOGGLES = {
    "members_can_add": ("Members can add options", False),
    "live_results": ("Live results", False),
    "allow_vote_change": ("Vote changes", False),
    "show_authors": ("Show who added options", True),
    "show_voters": ("Public votes", True),
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _plural(n: int, word: str, plural: str = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _bar(fraction: float, width: int = 10) -> str:
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _parse_duration(text: str):
    """'2d', '12h', '1d6h', '90m', '1.5h' -> seconds, or None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if not re.fullmatch(r"(\d+(\.\d+)?[dhm])+", text):
        return None
    units = {"d": 86400, "h": 3600, "m": 60}
    total = sum(float(n) * units[u] for n, _, u in re.findall(r"(\d+(\.\d+)?)([dhm])", text))
    return int(total) if total > 0 else None


def _normalize_record(record: dict) -> dict:
    """Fills defaults for any missing keys, so older/partial records render."""
    if "suggestions" in record and "options" not in record:  # pre-rename records
        record["options"] = record.pop("suggestions")
        record["max_adds"] = record.pop("max_suggestions", config.POLL_DEFAULT_MAX_ADDS_PER_MEMBER)
        record["members_can_add"] = True
        record["collect_first"] = True
        if record.get("phase") == "suggestions":
            record["phase"] = PHASE_ADDING
    defaults = {
        "description": None,
        "options": [],
        "next_id": len(record.get("options", [])) + 1,
        "votes": {},
        "members_can_add": False,
        "max_adds": config.POLL_DEFAULT_MAX_ADDS_PER_MEMBER,
        "collect_first": True,
        "max_votes": config.POLL_DEFAULT_MAX_VOTES_PER_MEMBER,
        "live_results": config.POLL_DEFAULT_LIVE_RESULTS,
        "show_authors": config.POLL_DEFAULT_SHOW_AUTHORS,
        "show_voters": config.POLL_DEFAULT_SHOW_VOTERS,
        "allow_vote_change": config.POLL_DEFAULT_ALLOW_VOTE_CHANGE,
        "duration": None,
        "closes_at": None,
    }
    for key, value in defaults.items():
        record.setdefault(key, value)
    return record


class AddOptionModal(discord.ui.Modal, title="Add an option"):
    option = discord.ui.TextInput(label="Option", required=True, min_length=1, max_length=OPTION_MAX_LENGTH)

    def __init__(self, cog: "PollsCog", poll_message, *, as_manager: bool, remaining=None):
        super().__init__()
        self.cog = cog
        self.poll_message = poll_message
        self.as_manager = as_manager
        if remaining is not None:
            self.option.placeholder = f"You can add {_plural(remaining, 'more option')}"

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._add_option(interaction, self.poll_message, str(self.option), as_manager=self.as_manager)


class PollsCog(commands.Cog):
    poll = app_commands.Group(name="poll", description="Create and run polls")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = config.MOD_ROLE_ID
        self.store = ApplicationStore(path="polls.json")
        # One lock per poll message: serializes "mutate record + re-render
        # message" so two near-simultaneous votes can't have their message
        # edits land out of order and show a stale tally.
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        self.bot.add_view(self._persistent_dummy_view())
        self.auto_close_loop.start()

    async def cog_unload(self):
        self.auto_close_loop.cancel()

    # --- helpers ------------------------------------------------------------

    def _lock(self, message_id: int) -> asyncio.Lock:
        return self._locks.setdefault(message_id, asyncio.Lock())

    def _get(self, message_id: int):
        record = self.store.get(message_id)
        return _normalize_record(record) if record is not None else None

    def _save(self, message_id: int, record: dict):
        self.store.set(message_id, **record)

    def _is_mod(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    def _can_manage(self, member, record: dict) -> bool:
        return self._is_mod(member) or member.id == record.get("created_by")

    async def _get_or_complain(self, interaction: discord.Interaction):
        record = self._get(interaction.message.id) if interaction.message else None
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this poll's record - it may have been removed.", ephemeral=True
            )
        return record

    async def _refresh(self, message):
        """Re-renders the public poll message from its current record."""
        record = self._get(message.id)
        if record is None:
            return
        try:
            await message.edit(view=self._render(record), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.exception("Failed to refresh poll message %s", message.id)

    @staticmethod
    def _own_options(record: dict, user_id: int) -> list:
        return [o for o in record["options"] if o.get("author_id") == user_id]

    @staticmethod
    def _tally(record: dict) -> dict:
        counts = {o["id"]: 0 for o in record["options"]}
        for ballot in record["votes"].values():
            for oid in ballot:
                if oid in counts:
                    counts[oid] += 1
        return counts

    @staticmethod
    def _voters_by_option(record: dict) -> dict:
        voters = {o["id"]: [] for o in record["options"]}
        for user_id, ballot in record["votes"].items():
            for oid in ballot:
                if oid in voters:
                    voters[oid].append(int(user_id))
        return voters

    @staticmethod
    def _results_visible(record: dict) -> bool:
        return record["phase"] == PHASE_CLOSED or (record["phase"] == PHASE_VOTING and record["live_results"])

    @staticmethod
    def _adding_open(record: dict) -> bool:
        if not record["members_can_add"]:
            return False
        if record["phase"] == PHASE_ADDING:
            return True
        return record["phase"] == PHASE_VOTING and not record["collect_first"]

    def _start_voting(self, record: dict):
        record["phase"] = PHASE_VOTING
        if record["duration"] and not record["closes_at"]:
            record["closes_at"] = int(time.time()) + record["duration"]

    def _option_description(self, record: dict, option: dict):
        if record["show_authors"] and option.get("author_name"):
            return f"Added by {option['author_name']}"[:100]
        return None

    # --- rendering ----------------------------------------------------------

    def _option_lines(self, record: dict, *, bars: bool, authors: bool, voters: bool) -> list:
        options = record["options"]
        if not self._results_visible(record):
            lines = []
            for i, o in enumerate(options, 1):
                line = f"`{i:>2}.` **{o['text']}**"
                if authors and o.get("author_id"):
                    line += f" · added by <@{o['author_id']}>"
                lines.append(line)
            return lines

        counts = self._tally(record)
        voter_map = self._voters_by_option(record)
        total = sum(counts.values()) or 1
        ordered = list(options)
        if record["phase"] == PHASE_CLOSED:
            ordered.sort(key=lambda o: -counts[o["id"]])  # stable: ties keep original order
        top = max(counts.values(), default=0)
        distinct = sorted({c for c in counts.values() if c > 0}, reverse=True)
        medals = ["🥇", "🥈", "🥉"]

        lines = []
        for i, o in enumerate(ordered, 1):
            n = counts[o["id"]]
            prefix = f"`{i:>2}.`"
            if record["phase"] == PHASE_CLOSED and n > 0 and distinct.index(n) < 3:
                prefix = medals[distinct.index(n)]
            name = f"**{o['text']}**" if (n == top and n > 0) else o["text"]
            line = f"{prefix} {name} — "
            if bars:
                line += f"`{_bar(n / total)}` "
            line += _plural(n, "vote")
            if authors and o.get("author_id"):
                line += f" · added by <@{o['author_id']}>"
            if voters and voter_map[o["id"]]:
                ids = voter_map[o["id"]]
                shown = " ".join(f"<@{uid}>" for uid in ids[:VOTERS_SHOWN_INLINE])
                more = f" +{len(ids) - VOTERS_SHOWN_INLINE} more" if len(ids) > VOTERS_SHOWN_INLINE else ""
                line += f"\n-# ↳ {shown}{more}"
            lines.append(line)
        return lines

    def _options_body(self, record: dict, budget: int) -> str:
        if not record["options"]:
            if self._adding_open(record):
                return "*No options yet - press **➕ Add option** to add the first one!*"
            return "*No options yet.*"
        authors = record["show_authors"]
        voters = record["show_voters"]
        # Degrade gracefully if the list doesn't fit: drop inline voter
        # names first (the 👥 Voters button still lists them), then bars,
        # then the "added by" credit.
        attempts = [
            dict(bars=True, authors=authors, voters=voters),
            dict(bars=True, authors=authors, voters=False),
            dict(bars=False, authors=authors, voters=False),
            dict(bars=False, authors=False, voters=False),
        ]
        for attempt in attempts:
            body = "\n".join(self._option_lines(record, **attempt))
            if len(body) <= budget:
                return body
        return body[: budget - 1] + "…"

    def _rules_text(self, record: dict) -> str:
        phase = record["phase"]
        if phase == PHASE_ADDING:
            head = (
                f"**➕ Adding options** - everyone can add up to "
                f"{_plural(record['max_adds'], 'option')}. Voting opens once adding closes."
            )
        elif phase == PHASE_VOTING:
            pick = "one option" if record["max_votes"] == 1 else f"up to {record['max_votes']} options"
            head = f"**🗳️ Voting is open** - pick {pick} from the dropdown."
            if self._adding_open(record):
                head += f" You can also add up to {_plural(record['max_adds'], 'option')} of your own."
        else:
            head = "**🔒 This poll is closed.** Final results:"

        chips = []
        if phase != PHASE_CLOSED:
            chips.append("👁️ live results" if record["live_results"] else "🙈 results hidden until close")
            chips.append("🔁 vote can be changed" if record["allow_vote_change"] else "🔒 votes are final")
        chips.append("📢 votes are public" if record["show_voters"] else "🕶️ votes are anonymous")
        if phase == PHASE_VOTING and record["closes_at"]:
            chips.append(f"⏰ closes <t:{record['closes_at']}:R>")
        elif phase == PHASE_ADDING and record["duration"]:
            chips.append(f"⏰ voting will run for {self._fmt_duration(record['duration'])}")
        return head + "\n-# " + " · ".join(chips)

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m" if minutes else ""]
        return "".join(parts) or "0m"

    def _render(self, record: dict) -> discord.ui.LayoutView:
        phase = record["phase"]
        color = {
            PHASE_ADDING: discord.Color.blurple(),
            PHASE_VOTING: discord.Color.gold(),
            PHASE_CLOSED: discord.Color.dark_grey(),
        }[phase]

        header = f"## 🗳️ {record['question']}"
        if record.get("description"):
            header += f"\n{record['description']}"
        rules = self._rules_text(record)
        voters = len(record["votes"])
        stats = f"-# {_plural(len(record['options']), 'option')}"
        if phase != PHASE_ADDING or voters:
            stats += f" · {_plural(voters, 'member')} voted"
        stats += f" · poll by <@{record['created_by']}>"
        footer = f"-# {config.POLL_FOOTER}" if config.POLL_FOOTER else ""
        budget = TEXT_BUDGET - len(header) - len(rules) - len(stats) - len(footer)

        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(rules))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._options_body(record, budget)))
        container.add_item(discord.ui.TextDisplay(stats))

        if phase == PHASE_VOTING and record["options"]:
            select = discord.ui.Select(
                custom_id=VOTE_SELECT_ID,
                placeholder="Cast your vote..." if record["max_votes"] == 1 else f"Pick up to {record['max_votes']}...",
                min_values=1,
                max_values=max(1, min(record["max_votes"], len(record["options"]))),
                options=[
                    discord.SelectOption(
                        label=o["text"], value=str(o["id"]), description=self._option_description(record, o)
                    )
                    for o in record["options"]
                ],
            )
            select.callback = self._on_vote
            container.add_item(discord.ui.ActionRow(select))

        row = discord.ui.ActionRow()
        if self._adding_open(record):
            row.add_item(self._button("➕ Add option", discord.ButtonStyle.success, ADD_ID, self._on_add))
            if phase == PHASE_ADDING:
                row.add_item(self._button("🗑️ Withdraw mine", discord.ButtonStyle.secondary, WITHDRAW_ID, self._on_withdraw))
        if phase == PHASE_VOTING:
            row.add_item(self._button("🧾 My vote", discord.ButtonStyle.secondary, MY_VOTES_ID, self._on_my_votes))
            if record["allow_vote_change"]:
                row.add_item(self._button("↩️ Clear vote", discord.ButtonStyle.secondary, CLEAR_VOTES_ID, self._on_clear_votes))
        if record["show_voters"] and self._results_visible(record) and record["votes"]:
            row.add_item(self._button("👥 Voters", discord.ButtonStyle.secondary, VOTERS_ID, self._on_voters))
        row.add_item(self._button("⚙️ Manage", discord.ButtonStyle.secondary, MANAGE_ID, self._on_manage))
        container.add_item(row)

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        if footer:
            view.add_item(discord.ui.TextDisplay(footer))
        return view

    @staticmethod
    def _button(label, style, custom_id, callback) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
        button.callback = callback
        return button

    def _persistent_dummy_view(self) -> discord.ui.LayoutView:
        """Registers every static custom_id this cog sends so buttons on
        polls posted before a restart keep working."""
        view = discord.ui.LayoutView(timeout=None)
        handlers = [
            (ADD_ID, self._on_add), (WITHDRAW_ID, self._on_withdraw), (MY_VOTES_ID, self._on_my_votes),
            (CLEAR_VOTES_ID, self._on_clear_votes), (VOTERS_ID, self._on_voters), (MANAGE_ID, self._on_manage),
        ]
        for start in range(0, len(handlers), 5):
            row = discord.ui.ActionRow()
            for custom_id, handler in handlers[start:start + 5]:
                row.add_item(self._button(custom_id, discord.ButtonStyle.secondary, custom_id, handler))
            view.add_item(row)
        select = discord.ui.Select(custom_id=VOTE_SELECT_ID, options=[discord.SelectOption(label="x")])
        select.callback = self._on_vote
        view.add_item(discord.ui.ActionRow(select))
        # Backwards compat: polls posted before the "suggestion" -> "option" rename.
        legacy = discord.ui.ActionRow()
        legacy.add_item(self._button("legacy", discord.ButtonStyle.secondary, "poll_suggest", self._on_add))
        view.add_item(legacy)
        return view

    # --- /poll create -------------------------------------------------------

    @poll.command(name="create", description="Start a poll - fixed options, member-added options, or both")
    @app_commands.describe(
        question="What the poll asks, e.g. 'Raid night?' or 'New guild name?'",
        description="Optional extra context shown under the question",
        options="Starting options, separated by ';' (e.g. 'Yes; No; Maybe'). Empty = members add them",
        members_can_add="Let members add their own options (default: on when no options are given)",
        max_adds="How many options each member may add",
        collect_first="Collect member options before voting opens (off = add and vote at the same time)",
        max_votes="How many options each member may vote for",
        live_results="Show vote counts while voting is open (off = revealed on close)",
        show_authors="Show who added each option",
        show_voters="Make votes public - show who voted for what",
        allow_vote_change="Let members change or withdraw their vote",
        duration="Auto-close voting after this long once it opens, e.g. 2d, 12h, 1d6h, 90m",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, QUESTION_MAX_LENGTH],
        description: app_commands.Range[str, 1, DESCRIPTION_MAX_LENGTH] = None,
        options: str = None,
        members_can_add: bool = None,
        max_adds: app_commands.Range[int, 1, MAX_OPTIONS] = None,
        collect_first: bool = None,
        max_votes: app_commands.Range[int, 1, MAX_OPTIONS] = None,
        live_results: bool = None,
        show_authors: bool = None,
        show_voters: bool = None,
        allow_vote_change: bool = None,
        duration: str = None,
    ):
        if config.POLL_CREATE_MOD_ONLY and not self._is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can create polls.", ephemeral=True)
            return
        if interaction.channel is None or interaction.guild is None:
            await interaction.response.send_message("Polls can only be created in a server channel.", ephemeral=True)
            return

        async def fail(msg):
            await interaction.response.send_message(msg, ephemeral=True)

        initial = []
        seen = set()
        for raw in (options or "").split(";"):
            text = _clean(raw)
            if not text or _normalize(text) in seen:
                continue
            if len(text) > OPTION_MAX_LENGTH:
                return await fail(f"Option `{text[:60]}…` is too long - keep each under {OPTION_MAX_LENGTH} characters.")
            seen.add(_normalize(text))
            initial.append({
                "id": len(initial) + 1, "text": text,
                "author_id": interaction.user.id, "author_name": interaction.user.display_name,
            })
        if len(initial) > MAX_OPTIONS:
            return await fail(f"At most {MAX_OPTIONS} options per poll.")

        if members_can_add is None:
            members_can_add = not initial
        if not members_can_add and len(initial) < 2:
            return await fail("Give at least 2 options, or turn on `members_can_add` so members can add their own.")

        duration_seconds = None
        if duration:
            duration_seconds = _parse_duration(duration)
            if duration_seconds is None:
                return await fail("Couldn't read that duration - use something like `2d`, `12h`, `1d6h` or `90m`.")
            if duration_seconds > MAX_DURATION_SECONDS:
                return await fail("Polls can run for at most 30 days.")

        record = _normalize_record({
            "question": _clean(question),
            "description": description.strip() if description else None,
            "created_by": interaction.user.id,
            "channel_id": interaction.channel.id,
            "options": initial,
            "next_id": len(initial) + 1,
            "members_can_add": members_can_add,
            "max_adds": max_adds or config.POLL_DEFAULT_MAX_ADDS_PER_MEMBER,
            "collect_first": config.POLL_DEFAULT_COLLECT_FIRST if collect_first is None else collect_first,
            "max_votes": max_votes or config.POLL_DEFAULT_MAX_VOTES_PER_MEMBER,
            "live_results": config.POLL_DEFAULT_LIVE_RESULTS if live_results is None else live_results,
            "show_authors": config.POLL_DEFAULT_SHOW_AUTHORS if show_authors is None else show_authors,
            "show_voters": config.POLL_DEFAULT_SHOW_VOTERS if show_voters is None else show_voters,
            "allow_vote_change": config.POLL_DEFAULT_ALLOW_VOTE_CHANGE if allow_vote_change is None else allow_vote_change,
            "duration": duration_seconds,
        })
        if members_can_add and record["collect_first"]:
            record["phase"] = PHASE_ADDING
        else:
            self._start_voting(record)

        try:
            message = await interaction.channel.send(view=self._render(record), allowed_mentions=NO_PINGS)
        except discord.Forbidden:
            return await fail("I don't have permission to post in this channel.")
        self._save(message.id, record)
        tip = "open voting and close it" if record["phase"] == PHASE_ADDING else "close it or tweak settings"
        await interaction.response.send_message(f"Poll posted! Use its **⚙️ Manage** button to {tip}.", ephemeral=True)

    # --- adding options -------------------------------------------------------

    async def _on_add(self, interaction: discord.Interaction):
        record = await self._get_or_complain(interaction)
        if record is None:
            return
        if not self._adding_open(record):
            await interaction.response.send_message("Adding options is closed for this poll.", ephemeral=True)
            return
        remaining = record["max_adds"] - len(self._own_options(record, interaction.user.id))
        if remaining <= 0:
            hint = " Use **🗑️ Withdraw mine** to swap one out." if record["phase"] == PHASE_ADDING else ""
            await interaction.response.send_message(
                f"You've already added your {_plural(record['max_adds'], 'option')}.{hint}", ephemeral=True
            )
            return
        if len(record["options"]) >= MAX_OPTIONS:
            await interaction.response.send_message(f"This poll already has the maximum of {MAX_OPTIONS} options.", ephemeral=True)
            return
        await interaction.response.send_modal(
            AddOptionModal(self, interaction.message, as_manager=False, remaining=remaining)
        )

    async def _add_option(self, interaction: discord.Interaction, message, raw: str, *, as_manager: bool):
        text = _clean(raw)
        async with self._lock(message.id):
            record = self._get(message.id)
            # Re-check everything: the poll may have changed while the modal was open.
            error = None
            if record is None:
                error = "This poll no longer exists."
            elif record["phase"] == PHASE_CLOSED:
                error = "This poll is closed."
            elif not as_manager and not self._adding_open(record):
                error = "Adding options closed while you were typing - sorry!"
            elif not text:
                error = "The option was empty."
            elif not as_manager and len(self._own_options(record, interaction.user.id)) >= record["max_adds"]:
                error = f"You've already added your {_plural(record['max_adds'], 'option')}."
            elif len(record["options"]) >= MAX_OPTIONS:
                error = f"This poll already has the maximum of {MAX_OPTIONS} options."
            elif any(_normalize(o["text"]) == _normalize(text) for o in record["options"]):
                error = f"**{text}** is already an option."
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

            record["options"].append({
                "id": record["next_id"],
                "text": text,
                "author_id": interaction.user.id,
                "author_name": interaction.user.display_name,
            })
            record["next_id"] += 1
            self._save(message.id, record)
            reply = f"Added **{text}**!"
            if not as_manager:
                left = record["max_adds"] - len(self._own_options(record, interaction.user.id))
                reply += f" You can add {_plural(left, 'more')}."
            await interaction.response.send_message(reply, ephemeral=True)
            await self._refresh(message)

    async def _on_withdraw(self, interaction: discord.Interaction):
        record = await self._get_or_complain(interaction)
        if record is None:
            return
        if record["phase"] != PHASE_ADDING:
            await interaction.response.send_message("Options are locked once voting opens.", ephemeral=True)
            return
        own = self._own_options(record, interaction.user.id)
        if not own:
            await interaction.response.send_message("You haven't added any options.", ephemeral=True)
            return
        view = self._removal_view(interaction.message, record, own, "Pick option(s) to withdraw...", only_author=interaction.user.id)
        await interaction.response.send_message("Which of your options should go?", view=view, ephemeral=True)

    def _removal_view(self, poll_message, record: dict, entries: list, placeholder: str, only_author=None):
        """Ephemeral one-off select for removing options - used both for a
        member withdrawing their own and a manager removing any."""
        view = discord.ui.View(timeout=300)
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=len(entries),
            options=[
                discord.SelectOption(label=o["text"], value=str(o["id"]), description=self._option_description(record, o))
                for o in entries
            ],
        )

        async def callback(interaction: discord.Interaction):
            ids = {int(v) for v in select.values}
            async with self._lock(poll_message.id):
                rec = self._get(poll_message.id)
                if rec is None:
                    await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                    return
                if only_author is not None and rec["phase"] != PHASE_ADDING:
                    await interaction.response.edit_message(content="Options are locked now.", view=None)
                    return
                removed = [
                    o for o in rec["options"]
                    if o["id"] in ids and (only_author is None or o.get("author_id") == only_author)
                ]
                removed_ids = {o["id"] for o in removed}
                rec["options"] = [o for o in rec["options"] if o["id"] not in removed_ids]
                for voter in list(rec["votes"]):
                    ballot = [oid for oid in rec["votes"][voter] if oid not in removed_ids]
                    if ballot:
                        rec["votes"][voter] = ballot
                    else:
                        del rec["votes"][voter]
                self._save(poll_message.id, rec)
                names = ", ".join(f"**{o['text']}**" for o in removed) or "nothing (already gone)"
                await interaction.response.edit_message(content=f"Removed {names}.", view=None)
                await self._refresh(poll_message)

        select.callback = callback
        view.add_item(select)
        return view

    # --- voting -------------------------------------------------------------

    async def _on_vote(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        async with self._lock(interaction.message.id):
            record = self._get(interaction.message.id)
            if record is None:
                await interaction.response.send_message("Couldn't find this poll's record.", ephemeral=True)
                return
            if record["phase"] != PHASE_VOTING:
                await interaction.response.send_message("Voting isn't open on this poll.", ephemeral=True)
                return
            key = str(interaction.user.id)
            had_ballot = key in record["votes"]
            if had_ballot and not record["allow_vote_change"]:
                await interaction.response.send_message("You've already voted, and votes on this poll are final.", ephemeral=True)
                await self._refresh(interaction.message)  # reset the dropdown
                return

            valid = {o["id"]: o for o in record["options"]}
            picked = []
            for v in values:
                try:
                    oid = int(v)
                except ValueError:
                    continue
                if oid in valid and oid not in picked:
                    picked.append(oid)
            picked = picked[: record["max_votes"]]
            if not picked:
                await interaction.response.send_message("That option no longer exists - try again.", ephemeral=True)
                await self._refresh(interaction.message)
                return

            record["votes"][key] = picked
            self._save(interaction.message.id, record)
            names = ", ".join(f"**{valid[oid]['text']}**" for oid in picked)
            verb = "Vote changed to" if had_ballot else "Voted for"
            note = "" if record["allow_vote_change"] else " (votes on this poll are final)"
            if record["show_voters"]:
                note += "\n-# Votes on this poll are public."
            await interaction.response.send_message(f"🗳️ {verb} {names}.{note}", ephemeral=True)
            # Re-rendering also resets the dropdown, so nobody's pick lingers on screen.
            await self._refresh(interaction.message)

    async def _on_my_votes(self, interaction: discord.Interaction):
        record = await self._get_or_complain(interaction)
        if record is None:
            return
        ballot = record["votes"].get(str(interaction.user.id))
        if not ballot:
            await interaction.response.send_message("You haven't voted on this poll.", ephemeral=True)
            return
        names = {o["id"]: o["text"] for o in record["options"]}
        listed = ", ".join(f"**{names[oid]}**" for oid in ballot if oid in names)
        await interaction.response.send_message(f"You voted for {listed}.", ephemeral=True)

    async def _on_clear_votes(self, interaction: discord.Interaction):
        async with self._lock(interaction.message.id):
            record = self._get(interaction.message.id)
            if record is None:
                await interaction.response.send_message("Couldn't find this poll's record.", ephemeral=True)
                return
            if record["phase"] != PHASE_VOTING:
                await interaction.response.send_message("Voting isn't open on this poll.", ephemeral=True)
                return
            if not record["allow_vote_change"]:
                await interaction.response.send_message("Votes on this poll are final.", ephemeral=True)
                return
            if record["votes"].pop(str(interaction.user.id), None) is None:
                await interaction.response.send_message("You haven't voted on this poll.", ephemeral=True)
                return
            self._save(interaction.message.id, record)
            await interaction.response.send_message("Your vote has been withdrawn.", ephemeral=True)
            await self._refresh(interaction.message)

    async def _on_voters(self, interaction: discord.Interaction):
        record = await self._get_or_complain(interaction)
        if record is None:
            return
        if not record["show_voters"] or not self._results_visible(record):
            await interaction.response.send_message("Votes on this poll aren't public.", ephemeral=True)
            return
        await interaction.response.send_message(self._full_breakdown(record, with_voters=True), ephemeral=True, allowed_mentions=NO_PINGS)

    def _full_breakdown(self, record: dict, *, with_voters: bool) -> str:
        counts = self._tally(record)
        voter_map = self._voters_by_option(record)
        lines = [f"**{record['question']}** - {_plural(len(record['votes']), 'member')} voted"]
        for o in sorted(record["options"], key=lambda o: -counts[o["id"]]):
            line = f"**{o['text']}** — {_plural(counts[o['id']], 'vote')}"
            if with_voters and voter_map[o["id"]]:
                line += ": " + ", ".join(f"<@{uid}>" for uid in voter_map[o["id"]])
            lines.append(line)
        text = "\n".join(lines)
        return text if len(text) <= 1990 else text[:1980] + "\n…"

    # --- closing ------------------------------------------------------------

    def _result_announcement(self, record: dict) -> str:
        counts = self._tally(record)
        top = max(counts.values(), default=0)
        if top == 0:
            return f"🔒 **{record['question']}** closed with no votes."
        winners = [o["text"] for o in record["options"] if counts[o["id"]] == top]
        votes = _plural(top, "vote")
        if len(winners) == 1:
            return f"🔒 **{record['question']}** has closed - **{winners[0]}** wins with {votes}!"
        tied = ", ".join(f"**{w}**" for w in winners[:-1]) + f" and **{winners[-1]}**"
        return f"🔒 **{record['question']}** has closed - it's a tie between {tied} ({votes} each)."

    async def _close(self, message, record: dict):
        """Caller holds the lock. Closes, saves, re-renders and announces."""
        record["phase"] = PHASE_CLOSED
        record["closes_at"] = None
        self._save(message.id, record)
        await self._refresh(message)
        try:
            await message.reply(self._result_announcement(record), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.exception("Failed to post result for poll %s", message.id)

    @tasks.loop(seconds=60)
    async def auto_close_loop(self):
        now = time.time()
        for key, raw in self.store.items():
            if raw.get("phase") != PHASE_VOTING or not raw.get("closes_at") or raw["closes_at"] > now:
                continue
            message_id = int(key)
            channel = self.bot.get_channel(raw.get("channel_id", 0))
            if channel is None:
                continue
            message = channel.get_partial_message(message_id)
            async with self._lock(message_id):
                record = self._get(message_id)
                if record and record["phase"] == PHASE_VOTING and record["closes_at"] and record["closes_at"] <= now:
                    log.info("Auto-closing poll %s", message_id)
                    await self._close(message, record)

    @auto_close_loop.before_loop
    async def _before_auto_close(self):
        await self.bot.wait_until_ready()

    # --- manage panel -------------------------------------------------------

    async def _on_manage(self, interaction: discord.Interaction):
        record = await self._get_or_complain(interaction)
        if record is None:
            return
        if not self._can_manage(interaction.user, record):
            await interaction.response.send_message("Only moderators or the poll's creator can manage it.", ephemeral=True)
            return
        await interaction.response.send_message(
            self._manage_text(record), view=self._manage_view(interaction.message, record),
            ephemeral=True, allowed_mentions=NO_PINGS,
        )

    def _manage_text(self, record: dict, note: str = None) -> str:
        on_off = lambda v: "on" if v else "off"  # noqa: E731
        lines = [
            f"**Managing:** {record['question']}",
            f"Phase: **{record['phase']}** · {_plural(len(record['options']), 'option')} · "
            f"{_plural(len(record['votes']), 'member')} voted",
            f"-# Members can add: {on_off(record['members_can_add'])} (max {record['max_adds']} each, "
            f"{'separate adding phase' if record['collect_first'] else 'while voting'}) · "
            f"votes per member: {record['max_votes']} · live results: {on_off(record['live_results'])} · "
            f"vote changes: {on_off(record['allow_vote_change'])} · show authors: {on_off(record['show_authors'])} · "
            f"public votes: {on_off(record['show_voters'])}",
        ]
        if record["closes_at"]:
            lines.append(f"-# Auto-closes <t:{record['closes_at']}:R>")
        elif record["duration"] and record["phase"] == PHASE_ADDING:
            lines.append(f"-# Voting will auto-close {self._fmt_duration(record['duration'])} after it opens")
        if note:
            lines.insert(0, note + "\n")
        return "\n".join(lines)

    def _manage_view(self, poll_message, record: dict) -> discord.ui.View:
        view = discord.ui.View(timeout=600)
        phase = record["phase"]

        def add(label, style, handler, row):
            button = discord.ui.Button(label=label, style=style, row=row)

            async def callback(interaction: discord.Interaction):
                await handler(interaction, poll_message)

            button.callback = callback
            view.add_item(button)

        # Row 0: phase control
        if phase == PHASE_ADDING:
            add("🗳️ Open voting", discord.ButtonStyle.success, self._manage_open_voting, 0)
        if phase == PHASE_VOTING:
            add("🔒 Close poll", discord.ButtonStyle.danger, self._manage_close, 0)
            if record["members_can_add"] and record["collect_first"]:
                add("➕ Reopen adding", discord.ButtonStyle.secondary, self._manage_reopen_adding, 0)
        if phase == PHASE_CLOSED:
            add("🗳️ Reopen voting", discord.ButtonStyle.secondary, self._manage_reopen_voting, 0)
        if phase == PHASE_VOTING and record["closes_at"]:
            add("⏰ Remove deadline", discord.ButtonStyle.secondary, self._manage_clear_deadline, 0)

        # Row 1: options
        if phase != PHASE_CLOSED:
            add("➕ Add option", discord.ButtonStyle.secondary, self._manage_add_option, 1)
            if record["options"]:
                add("🧹 Remove options", discord.ButtonStyle.secondary, self._manage_remove, 1)
        if not self._results_visible(record) and record["votes"]:
            add("📊 Peek at results", discord.ButtonStyle.secondary, self._manage_peek, 1)

        # Rows 2-3: setting toggles
        for i, (key, (label, one_way)) in enumerate(TOGGLES.items()):
            if one_way and not record[key]:
                continue  # privacy settings can't be turned back on
            if key in ("members_can_add",) and phase == PHASE_CLOSED:
                continue
            state = "on" if record[key] else "off"
            add(f"{label}: {state}", discord.ButtonStyle.secondary, self._toggle_handler(key), 2 + i // 3)
        return view

    def _toggle_handler(self, key: str):
        async def handler(interaction, poll_message):
            async with self._lock(poll_message.id):
                record = self._get(poll_message.id)
                if record is None:
                    await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                    return
                label, one_way = TOGGLES[key]
                if one_way and not record[key]:
                    await interaction.response.send_message("That privacy setting can't be turned back on.", ephemeral=True)
                    return
                record[key] = not record[key]
                if key == "members_can_add" and not record[key] and record["phase"] == PHASE_ADDING:
                    if len(record["options"]) >= 2:
                        self._start_voting(record)
                    else:
                        record[key] = True
                        await interaction.response.send_message(
                            "Add at least 2 options before turning member adding off.", ephemeral=True
                        )
                        return
                self._save(poll_message.id, record)
                note = f"✅ {label}: **{'on' if record[key] else 'off'}**"
                await interaction.response.edit_message(
                    content=self._manage_text(record, note), view=self._manage_view(poll_message, record),
                    allowed_mentions=NO_PINGS,
                )
                await self._refresh(poll_message)
        return handler

    async def _update_phase(self, interaction, poll_message, mutate, ok_text, close=False):
        async with self._lock(poll_message.id):
            record = self._get(poll_message.id)
            if record is None:
                await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                return
            error = mutate(record)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            await interaction.response.edit_message(
                content=self._manage_text(record, ok_text), view=self._manage_view(poll_message, record),
                allowed_mentions=NO_PINGS,
            )
            if close:
                await self._close(poll_message, record)
            else:
                self._save(poll_message.id, record)
                await self._refresh(poll_message)

    async def _manage_open_voting(self, interaction, poll_message):
        def mutate(record):
            if len(record["options"]) < 2:
                return "Need at least 2 options before voting can open."
            self._start_voting(record)
        await self._update_phase(interaction, poll_message, mutate, "✅ Voting is open - options are now locked.")

    async def _manage_close(self, interaction, poll_message):
        def mutate(record):
            record["phase"] = PHASE_CLOSED
            record["closes_at"] = None
        await self._update_phase(interaction, poll_message, mutate, "🔒 Poll closed - results posted.", close=True)

    async def _manage_reopen_adding(self, interaction, poll_message):
        def mutate(record):
            record["phase"] = PHASE_ADDING
            record["closes_at"] = None
        await self._update_phase(
            interaction, poll_message, mutate,
            "➕ Adding reopened. Existing votes are kept; reopen voting when ready.",
        )

    async def _manage_reopen_voting(self, interaction, poll_message):
        def mutate(record):
            record["phase"] = PHASE_VOTING
            record["closes_at"] = None
        await self._update_phase(interaction, poll_message, mutate, "🗳️ Voting reopened (no deadline).")

    async def _manage_clear_deadline(self, interaction, poll_message):
        def mutate(record):
            record["closes_at"] = None
        await self._update_phase(interaction, poll_message, mutate, "⏰ Deadline removed - close the poll manually.")

    async def _manage_add_option(self, interaction, poll_message):
        record = self._get(poll_message.id)
        if record is None or record["phase"] == PHASE_CLOSED:
            await interaction.response.send_message("This poll is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(AddOptionModal(self, poll_message, as_manager=True))

    async def _manage_remove(self, interaction, poll_message):
        record = self._get(poll_message.id)
        if record is None or not record["options"]:
            await interaction.response.send_message("There's nothing to remove.", ephemeral=True)
            return
        view = self._removal_view(poll_message, record, record["options"], "Pick options to remove...")
        await interaction.response.send_message(
            "Which options should be removed? Any votes for them are dropped too.", view=view, ephemeral=True
        )

    async def _manage_peek(self, interaction, poll_message):
        record = self._get(poll_message.id)
        if record is None:
            await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
            return
        # Counts only - who voted for what stays private unless votes are public.
        text = self._full_breakdown(record, with_voters=record["show_voters"])
        await interaction.response.send_message(f"📊 Current standings (only you can see this):\n{text}", ephemeral=True, allowed_mentions=NO_PINGS)

    # --- cleanup ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if self.store.get(payload.message_id) is not None:
            self.store.delete(payload.message_id)
            self._locks.pop(payload.message_id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))
