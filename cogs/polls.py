"""
Polls feature - community suggestion polls (and plain fixed-option polls).

Built for the "we need a new guild name" use case, but generic:

  1. Someone (a moderator by default - see config.POLL_CREATE_MOD_ONLY) runs
     `/poll create`, giving a question and optionally a description, a
     per-member suggestion cap, a per-member vote cap, and whether live
     results are shown.
  2. SUGGESTION PHASE: the poll message carries a "💡 Suggest" button. Each
     member can submit up to `max_suggestions` entries (default
     config.POLL_DEFAULT_MAX_SUGGESTIONS_PER_USER) via a small modal, and
     withdraw their own entries again with "🗑️ Withdraw mine". Duplicates
     (case/whitespace-insensitive) are rejected, and the total is capped at
     25 - Discord's limit on options in one dropdown, which is what voting
     uses.
  3. VOTING PHASE: a moderator (or the poll's creator) opens voting from the
     "⚙️ Manage" panel. Submissions freeze and a dropdown appears; each
     member picks up to `max_votes` entries. Picking again REPLACES that
     member's ballot rather than adding to it, so changing your mind is just
     re-picking. "🧾 My votes" / "↩️ Clear my votes" cover the rest.
  4. CLOSED: the manage panel's "Close poll" freezes everything and shows
     the final ranking.

Passing `options:` to `/poll create` (semicolon-separated) skips the
suggestion phase entirely and opens straight into voting - a plain poll.

Everything the public sees lives on one Components V2 message, re-rendered
after every change. Ballots are stored per member but only totals are ever
shown - who voted for what isn't displayed anywhere. Every poll message ends
with config.POLL_FOOTER.

Button/select custom_ids are static and the poll is looked up from
interaction.message.id (same pattern as cogs/announcements.py), so one
dummy view registered in cog_load keeps every poll's buttons alive across
restarts. Records live in polls.json via the shared storage.py store.
"""

import os
import re
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import ApplicationStore

log = logging.getLogger("wow-apply-bot.polls")

# Discord caps a select menu at 25 options - voting is one select, so that's
# the hard ceiling on how many entries one poll can have.
MAX_ENTRIES = 25
ENTRY_MAX_LENGTH = 50
QUESTION_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 400

PHASE_SUGGESTIONS = "suggestions"
PHASE_VOTING = "voting"
PHASE_CLOSED = "closed"

SUGGEST_ID = "poll_suggest"
WITHDRAW_ID = "poll_withdraw"
VOTE_SELECT_ID = "poll_vote_select"
MY_VOTES_ID = "poll_my_votes"
CLEAR_VOTES_ID = "poll_clear_votes"
MANAGE_ID = "poll_manage"

NO_PINGS = discord.AllowedMentions.none()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _plural(n: int, word: str, plural: str = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _bar(fraction: float, width: int = 10) -> str:
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


class SuggestModal(discord.ui.Modal, title="Submit a suggestion"):
    suggestion = discord.ui.TextInput(
        label="Your suggestion",
        required=True,
        min_length=1,
        max_length=ENTRY_MAX_LENGTH,
    )

    def __init__(self, cog: "PollsCog", poll_message: discord.Message, remaining: int):
        super().__init__()
        self.cog = cog
        self.poll_message = poll_message
        self.suggestion.placeholder = f"You can submit {_plural(remaining, 'more suggestion')}"

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._add_suggestion(interaction, self.poll_message, str(self.suggestion))


class PollsCog(commands.Cog):
    poll = app_commands.Group(name="poll", description="Create and run polls")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mod_role_id = int(os.environ["MOD_ROLE_ID"])
        self.store = ApplicationStore(path="polls.json")
        # One lock per poll message: serializes "mutate record + re-render
        # message" so two near-simultaneous votes can't have their message
        # edits land out of order and show a stale tally.
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        self.bot.add_view(self._persistent_dummy_view())

    # --- helpers ------------------------------------------------------------

    def _lock(self, message_id: int) -> asyncio.Lock:
        return self._locks.setdefault(message_id, asyncio.Lock())

    def _is_mod(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_roles:
            return True
        return any(role.id == self.mod_role_id for role in member.roles)

    def _can_manage(self, member, record: dict) -> bool:
        return self._is_mod(member) or member.id == record.get("created_by")

    async def _get_record(self, interaction: discord.Interaction):
        record = self.store.get(interaction.message.id) if interaction.message else None
        if record is None:
            await interaction.response.send_message(
                "Couldn't find this poll's record - it may have been removed.", ephemeral=True
            )
        return record

    async def _refresh(self, message: discord.Message):
        """Re-renders the public poll message from its current record."""
        record = self.store.get(message.id)
        if record is None:
            return
        try:
            await message.edit(view=self._render(record), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.exception("Failed to refresh poll message %s", message.id)

    @staticmethod
    def _tally(record: dict) -> dict:
        counts = {s["id"]: 0 for s in record["suggestions"]}
        for ballot in record["votes"].values():
            for sid in ballot:
                if sid in counts:
                    counts[sid] += 1
        return counts

    # --- rendering ----------------------------------------------------------

    def _entry_lines(self, record: dict, with_authors: bool) -> list:
        suggestions = record["suggestions"]
        phase = record["phase"]
        show_counts = phase == PHASE_CLOSED or (phase == PHASE_VOTING and record["live_results"])

        if not show_counts:
            lines = []
            for i, s in enumerate(suggestions, 1):
                line = f"`{i:>2}.` **{s['text']}**"
                if with_authors and s.get("author_id"):
                    line += f" · <@{s['author_id']}>"
                lines.append(line)
            return lines

        counts = self._tally(record)
        total = sum(counts.values()) or 1
        ordered = list(suggestions)
        if phase == PHASE_CLOSED:
            ordered.sort(key=lambda s: -counts[s["id"]])  # stable: ties keep submission order
        top = max(counts.values(), default=0)
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}

        lines = []
        for i, s in enumerate(ordered):
            n = counts[s["id"]]
            if phase == PHASE_CLOSED and n > 0:
                rank = sorted({c for c in counts.values() if c > 0}, reverse=True).index(n)
                prefix = medals.get(rank, f"`{i + 1:>2}.`")
            else:
                prefix = f"`{i + 1:>2}.`"
            name = f"**{s['text']}**" if (n == top and n > 0) else s["text"]
            line = f"{prefix} {name} — `{_bar(n / total)}` {_plural(n, 'vote')}"
            if with_authors and s.get("author_id"):
                line += f" · <@{s['author_id']}>"
            lines.append(line)
        return lines

    def _phase_text(self, record: dict) -> str:
        phase = record["phase"]
        max_s = record["max_suggestions"]
        max_v = record["max_votes"]
        if phase == PHASE_SUGGESTIONS:
            return (
                f"**📥 Suggestions are open** - press **💡 Suggest** to submit up to "
                f"{_plural(max_s, 'entry', 'entries')}. "
                "Voting opens once submissions close."
            )
        if phase == PHASE_VOTING:
            votes = "one entry" if max_v == 1 else f"up to {max_v} entries"
            text = (
                f"**🗳️ Voting is open** - pick {votes} from the dropdown. "
                "Picking again replaces your previous vote."
            )
            if not record["live_results"]:
                text += "\n-# Results are hidden until the poll closes."
            return text
        return "**🔒 This poll is closed.** Final results:"

    def _render(self, record: dict) -> discord.ui.LayoutView:
        phase = record["phase"]
        color = {
            PHASE_SUGGESTIONS: discord.Color.blurple(),
            PHASE_VOTING: discord.Color.gold(),
            PHASE_CLOSED: discord.Color.dark_grey(),
        }[phase]

        container = discord.ui.Container(accent_color=color)
        header = f"## 🗳️ {record['question']}"
        if record.get("description"):
            header += f"\n{record['description']}"
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._phase_text(record)))
        container.add_item(discord.ui.Separator())

        if record["suggestions"]:
            lines = self._entry_lines(record, with_authors=True)
            body = "\n".join(lines)
            if len(body) > 2600:  # stay well clear of the 4000-char message cap
                body = "\n".join(self._entry_lines(record, with_authors=False))
        else:
            body = "*No suggestions yet - be the first!*"
        container.add_item(discord.ui.TextDisplay(body))

        voters = len(record["votes"])
        stats = f"-# {_plural(len(record['suggestions']), 'entry', 'entries')}"
        if phase != PHASE_SUGGESTIONS or voters:
            stats += f" · {_plural(voters, 'member')} voted"
        container.add_item(discord.ui.TextDisplay(stats))

        if phase == PHASE_SUGGESTIONS:
            row = discord.ui.ActionRow()
            row.add_item(self._button("💡 Suggest", discord.ButtonStyle.success, SUGGEST_ID, self._on_suggest))
            row.add_item(self._button("🗑️ Withdraw mine", discord.ButtonStyle.secondary, WITHDRAW_ID, self._on_withdraw))
            row.add_item(self._button("⚙️ Manage", discord.ButtonStyle.secondary, MANAGE_ID, self._on_manage))
            container.add_item(row)
        elif phase == PHASE_VOTING:
            select = discord.ui.Select(
                custom_id=VOTE_SELECT_ID,
                placeholder="Cast your vote..." if record["max_votes"] == 1 else f"Pick up to {record['max_votes']}...",
                min_values=1,
                max_values=max(1, min(record["max_votes"], len(record["suggestions"]))),
                options=[
                    discord.SelectOption(
                        label=s["text"],
                        value=str(s["id"]),
                        description=f"Suggested by {s['author_name']}"[:100] if s.get("author_name") else None,
                    )
                    for s in record["suggestions"]
                ],
            )
            select.callback = self._on_vote
            container.add_item(discord.ui.ActionRow(select))
            row = discord.ui.ActionRow()
            row.add_item(self._button("🧾 My votes", discord.ButtonStyle.secondary, MY_VOTES_ID, self._on_my_votes))
            row.add_item(self._button("↩️ Clear my votes", discord.ButtonStyle.secondary, CLEAR_VOTES_ID, self._on_clear_votes))
            row.add_item(self._button("⚙️ Manage", discord.ButtonStyle.secondary, MANAGE_ID, self._on_manage))
            container.add_item(row)
        else:
            row = discord.ui.ActionRow()
            row.add_item(self._button("⚙️ Manage", discord.ButtonStyle.secondary, MANAGE_ID, self._on_manage))
            container.add_item(row)

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        if config.POLL_FOOTER:
            view.add_item(discord.ui.TextDisplay(f"-# {config.POLL_FOOTER}"))
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
        row = discord.ui.ActionRow()
        row.add_item(self._button("s", discord.ButtonStyle.secondary, SUGGEST_ID, self._on_suggest))
        row.add_item(self._button("w", discord.ButtonStyle.secondary, WITHDRAW_ID, self._on_withdraw))
        row.add_item(self._button("m", discord.ButtonStyle.secondary, MY_VOTES_ID, self._on_my_votes))
        row.add_item(self._button("c", discord.ButtonStyle.secondary, CLEAR_VOTES_ID, self._on_clear_votes))
        row.add_item(self._button("g", discord.ButtonStyle.secondary, MANAGE_ID, self._on_manage))
        view.add_item(row)
        select = discord.ui.Select(custom_id=VOTE_SELECT_ID, options=[discord.SelectOption(label="x")])
        select.callback = self._on_vote
        view.add_item(discord.ui.ActionRow(select))
        return view

    # --- /poll create -------------------------------------------------------

    @poll.command(name="create", description="Start a poll - members suggest entries, then everyone votes")
    @app_commands.describe(
        question="What the poll is about, e.g. 'New guild name?'",
        description="Optional extra context shown under the question",
        max_suggestions="How many entries each member may submit (default from config)",
        max_votes="How many entries each member may vote for (default from config)",
        live_results="Show vote counts while voting is open (default from config)",
        options="Skip suggestions: semicolon-separated fixed options, e.g. 'Yes; No; Maybe'",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, QUESTION_MAX_LENGTH],
        description: app_commands.Range[str, 1, DESCRIPTION_MAX_LENGTH] = None,
        max_suggestions: app_commands.Range[int, 1, MAX_ENTRIES] = None,
        max_votes: app_commands.Range[int, 1, MAX_ENTRIES] = None,
        live_results: bool = None,
        options: str = None,
    ):
        if config.POLL_CREATE_MOD_ONLY and not self._is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can create polls.", ephemeral=True)
            return
        if interaction.channel is None or interaction.guild is None:
            await interaction.response.send_message("Polls can only be created in a server channel.", ephemeral=True)
            return

        suggestions = []
        if options:
            seen = set()
            for raw in options.split(";"):
                text = _clean(raw)
                if not text or _normalize(text) in seen:
                    continue
                if len(text) > ENTRY_MAX_LENGTH:
                    await interaction.response.send_message(
                        f"Option `{text[:60]}…` is too long - keep each under {ENTRY_MAX_LENGTH} characters.",
                        ephemeral=True,
                    )
                    return
                seen.add(_normalize(text))
                suggestions.append({"id": len(suggestions) + 1, "text": text, "author_id": None, "author_name": None})
            if len(suggestions) < 2:
                await interaction.response.send_message("A fixed-option poll needs at least 2 distinct options.", ephemeral=True)
                return
            if len(suggestions) > MAX_ENTRIES:
                await interaction.response.send_message(f"At most {MAX_ENTRIES} options per poll.", ephemeral=True)
                return

        record = {
            "question": _clean(question),
            "description": description.strip() if description else None,
            "created_by": interaction.user.id,
            "channel_id": interaction.channel.id,
            "phase": PHASE_VOTING if suggestions else PHASE_SUGGESTIONS,
            "max_suggestions": max_suggestions or config.POLL_DEFAULT_MAX_SUGGESTIONS_PER_USER,
            "max_votes": max_votes or config.POLL_DEFAULT_MAX_VOTES_PER_USER,
            "live_results": config.POLL_DEFAULT_SHOW_LIVE_RESULTS if live_results is None else live_results,
            "suggestions": suggestions,
            "next_id": len(suggestions) + 1,
            "votes": {},
        }

        try:
            message = await interaction.channel.send(view=self._render(record), allowed_mentions=NO_PINGS)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to post in this channel.", ephemeral=True)
            return
        self.store.set(message.id, **record)
        await interaction.response.send_message(
            "Poll posted! Use its **⚙️ Manage** button to open voting and close it.", ephemeral=True
        )

    # --- suggestion phase -----------------------------------------------------

    def _own_suggestions(self, record: dict, user_id: int) -> list:
        return [s for s in record["suggestions"] if s.get("author_id") == user_id]

    async def _on_suggest(self, interaction: discord.Interaction):
        record = await self._get_record(interaction)
        if record is None:
            return
        if record["phase"] != PHASE_SUGGESTIONS:
            await interaction.response.send_message("Suggestions are closed for this poll.", ephemeral=True)
            return
        remaining = record["max_suggestions"] - len(self._own_suggestions(record, interaction.user.id))
        if remaining <= 0:
            await interaction.response.send_message(
                f"You've already submitted your {_plural(record['max_suggestions'], 'suggestion')}. "
                "Use **🗑️ Withdraw mine** to swap one out.",
                ephemeral=True,
            )
            return
        if len(record["suggestions"]) >= MAX_ENTRIES:
            await interaction.response.send_message(
                f"This poll already has the maximum of {MAX_ENTRIES} entries.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SuggestModal(self, interaction.message, remaining))

    async def _add_suggestion(self, interaction: discord.Interaction, message: discord.Message, raw: str):
        text = _clean(raw)
        async with self._lock(message.id):
            record = self.store.get(message.id)
            # Re-check everything: the poll may have changed while the modal was open.
            error = None
            if record is None:
                error = "This poll no longer exists."
            elif record["phase"] != PHASE_SUGGESTIONS:
                error = "Suggestions closed while you were typing - sorry!"
            elif not text:
                error = "Your suggestion was empty."
            elif len(self._own_suggestions(record, interaction.user.id)) >= record["max_suggestions"]:
                error = f"You've already submitted your {_plural(record['max_suggestions'], 'suggestion')}."
            elif len(record["suggestions"]) >= MAX_ENTRIES:
                error = f"This poll already has the maximum of {MAX_ENTRIES} entries."
            elif any(_normalize(s["text"]) == _normalize(text) for s in record["suggestions"]):
                error = f"**{text}** has already been suggested."
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

            record["suggestions"].append({
                "id": record["next_id"],
                "text": text,
                "author_id": interaction.user.id,
                "author_name": interaction.user.display_name,
            })
            record["next_id"] += 1
            self.store.set(message.id, **record)
            left = record["max_suggestions"] - len(self._own_suggestions(record, interaction.user.id))
            await interaction.response.send_message(
                f"Added **{text}**! You can submit {_plural(left, 'more')}.", ephemeral=True
            )
            await self._refresh(message)

    async def _on_withdraw(self, interaction: discord.Interaction):
        record = await self._get_record(interaction)
        if record is None:
            return
        if record["phase"] != PHASE_SUGGESTIONS:
            await interaction.response.send_message(
                "Suggestions are locked once voting opens.", ephemeral=True
            )
            return
        own = self._own_suggestions(record, interaction.user.id)
        if not own:
            await interaction.response.send_message("You haven't suggested anything yet.", ephemeral=True)
            return
        view = self._removal_view(
            interaction.message, own, placeholder="Pick suggestion(s) to withdraw...", only_author=interaction.user.id
        )
        await interaction.response.send_message("Which of your suggestions should go?", view=view, ephemeral=True)

    def _removal_view(self, poll_message: discord.Message, entries: list, placeholder: str, only_author=None):
        """Ephemeral one-off select for removing entries - used both for a
        member withdrawing their own and a manager removing any."""
        view = discord.ui.View(timeout=300)
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=len(entries),
            options=[
                discord.SelectOption(
                    label=s["text"],
                    value=str(s["id"]),
                    description=f"Suggested by {s['author_name']}"[:100] if s.get("author_name") else None,
                )
                for s in entries
            ],
        )

        async def callback(interaction: discord.Interaction):
            ids = {int(v) for v in select.values}
            async with self._lock(poll_message.id):
                record = self.store.get(poll_message.id)
                if record is None:
                    await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                    return
                if only_author is not None and record["phase"] != PHASE_SUGGESTIONS:
                    await interaction.response.edit_message(content="Suggestions are locked now.", view=None)
                    return
                removed = [
                    s for s in record["suggestions"]
                    if s["id"] in ids and (only_author is None or s.get("author_id") == only_author)
                ]
                removed_ids = {s["id"] for s in removed}
                record["suggestions"] = [s for s in record["suggestions"] if s["id"] not in removed_ids]
                for voter in list(record["votes"]):
                    ballot = [sid for sid in record["votes"][voter] if sid not in removed_ids]
                    if ballot:
                        record["votes"][voter] = ballot
                    else:
                        del record["votes"][voter]
                self.store.set(poll_message.id, **record)
                names = ", ".join(f"**{s['text']}**" for s in removed) or "nothing (already gone)"
                await interaction.response.edit_message(content=f"Removed {names}.", view=None)
                await self._refresh(poll_message)

        select.callback = callback
        view.add_item(select)
        return view

    # --- voting phase -----------------------------------------------------------

    async def _on_vote(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        async with self._lock(interaction.message.id):
            record = self.store.get(interaction.message.id)
            if record is None:
                await interaction.response.send_message("Couldn't find this poll's record.", ephemeral=True)
                return
            if record["phase"] != PHASE_VOTING:
                await interaction.response.send_message("Voting isn't open on this poll.", ephemeral=True)
                return
            valid = {s["id"]: s for s in record["suggestions"]}
            picked = []
            for v in values:
                try:
                    sid = int(v)
                except ValueError:
                    continue
                if sid in valid and sid not in picked:
                    picked.append(sid)
            picked = picked[: record["max_votes"]]
            if not picked:
                await interaction.response.send_message("That entry no longer exists - try again.", ephemeral=True)
                await self._refresh(interaction.message)
                return

            had_ballot = str(interaction.user.id) in record["votes"]
            record["votes"][str(interaction.user.id)] = picked
            self.store.set(interaction.message.id, **record)
            names = ", ".join(f"**{valid[sid]['text']}**" for sid in picked)
            verb = "Vote changed to" if had_ballot else "Voted for"
            await interaction.response.send_message(f"🗳️ {verb} {names}.", ephemeral=True)
            # Re-rendering also resets the dropdown, so nobody's pick lingers on screen.
            await self._refresh(interaction.message)

    async def _on_my_votes(self, interaction: discord.Interaction):
        record = await self._get_record(interaction)
        if record is None:
            return
        ballot = record["votes"].get(str(interaction.user.id))
        if not ballot:
            await interaction.response.send_message("You haven't voted on this poll.", ephemeral=True)
            return
        names = {s["id"]: s["text"] for s in record["suggestions"]}
        listed = ", ".join(f"**{names[sid]}**" for sid in ballot if sid in names)
        await interaction.response.send_message(f"You voted for {listed}.", ephemeral=True)

    async def _on_clear_votes(self, interaction: discord.Interaction):
        async with self._lock(interaction.message.id):
            record = self.store.get(interaction.message.id)
            if record is None:
                await interaction.response.send_message("Couldn't find this poll's record.", ephemeral=True)
                return
            if record["phase"] != PHASE_VOTING:
                await interaction.response.send_message("Voting isn't open on this poll.", ephemeral=True)
                return
            if record["votes"].pop(str(interaction.user.id), None) is None:
                await interaction.response.send_message("You haven't voted on this poll.", ephemeral=True)
                return
            self.store.set(interaction.message.id, **record)
            await interaction.response.send_message("Your vote has been withdrawn.", ephemeral=True)
            await self._refresh(interaction.message)

    # --- manage panel -----------------------------------------------------------

    async def _on_manage(self, interaction: discord.Interaction):
        record = await self._get_record(interaction)
        if record is None:
            return
        if not self._can_manage(interaction.user, record):
            await interaction.response.send_message(
                "Only moderators or the poll's creator can manage it.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            self._manage_text(record), view=self._manage_view(interaction.message, record), ephemeral=True
        )

    @staticmethod
    def _manage_text(record: dict) -> str:
        return (
            f"**Managing:** {record['question']}\n"
            f"Phase: **{record['phase']}** · max {record['max_suggestions']} suggestion(s) / "
            f"{record['max_votes']} vote(s) per member · live results {'on' if record['live_results'] else 'off'}"
        )

    def _manage_view(self, poll_message: discord.Message, record: dict) -> discord.ui.View:
        view = discord.ui.View(timeout=600)
        phase = record["phase"]

        def add(label, style, handler):
            button = discord.ui.Button(label=label, style=style)

            async def callback(interaction: discord.Interaction):
                await handler(interaction, poll_message)

            button.callback = callback
            view.add_item(button)

        if phase == PHASE_SUGGESTIONS:
            add("🗳️ Open voting", discord.ButtonStyle.success, self._manage_open_voting)
        if phase == PHASE_VOTING:
            add("🔒 Close poll", discord.ButtonStyle.danger, self._manage_close)
            add("📥 Reopen suggestions", discord.ButtonStyle.secondary, self._manage_reopen_suggestions)
        if phase == PHASE_CLOSED:
            add("🗳️ Reopen voting", discord.ButtonStyle.secondary, self._manage_reopen_voting)
        if phase != PHASE_CLOSED and record["suggestions"]:
            add("🧹 Remove entries", discord.ButtonStyle.secondary, self._manage_remove)
        add("👁️ Toggle live results", discord.ButtonStyle.secondary, self._manage_toggle_results)
        return view

    async def _set_phase(self, interaction, poll_message, new_phase, ok_text, check=None):
        async with self._lock(poll_message.id):
            record = self.store.get(poll_message.id)
            if record is None:
                await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                return
            if check:
                error = check(record)
                if error:
                    await interaction.response.send_message(error, ephemeral=True)
                    return
            record["phase"] = new_phase
            self.store.set(poll_message.id, **record)
            await interaction.response.edit_message(
                content=f"{ok_text}\n\n{self._manage_text(record)}", view=self._manage_view(poll_message, record)
            )
            await self._refresh(poll_message)

    async def _manage_open_voting(self, interaction, poll_message):
        def check(record):
            if len(record["suggestions"]) < 2:
                return "Need at least 2 entries before voting can open."
            return None
        await self._set_phase(interaction, poll_message, PHASE_VOTING, "✅ Voting is open - suggestions are now locked.", check)

    async def _manage_close(self, interaction, poll_message):
        await self._set_phase(interaction, poll_message, PHASE_CLOSED, "🔒 Poll closed - final results are posted.")

    async def _manage_reopen_suggestions(self, interaction, poll_message):
        await self._set_phase(
            interaction, poll_message, PHASE_SUGGESTIONS,
            "📥 Suggestions reopened (existing votes are kept, voting resumes when you reopen it).",
        )

    async def _manage_reopen_voting(self, interaction, poll_message):
        await self._set_phase(interaction, poll_message, PHASE_VOTING, "🗳️ Voting reopened.")

    async def _manage_toggle_results(self, interaction, poll_message):
        async with self._lock(poll_message.id):
            record = self.store.get(poll_message.id)
            if record is None:
                await interaction.response.edit_message(content="This poll no longer exists.", view=None)
                return
            record["live_results"] = not record["live_results"]
            self.store.set(poll_message.id, **record)
            await interaction.response.edit_message(
                content=f"Live results {'shown' if record['live_results'] else 'hidden'}.\n\n{self._manage_text(record)}",
                view=self._manage_view(poll_message, record),
            )
            await self._refresh(poll_message)

    async def _manage_remove(self, interaction, poll_message):
        record = self.store.get(poll_message.id)
        if record is None or not record["suggestions"]:
            await interaction.response.send_message("There's nothing to remove.", ephemeral=True)
            return
        view = self._removal_view(poll_message, record["suggestions"], placeholder="Pick entries to remove...")
        await interaction.response.send_message(
            "Which entries should be removed? Any votes for them are dropped too.", view=view, ephemeral=True
        )

    # --- cleanup ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if self.store.get(payload.message_id) is not None:
            self.store.delete(payload.message_id)
            self._locks.pop(payload.message_id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))
