# WoW TBC Fresh Guild Application Bot

## Architecture

```
bot.py          - entry point: sets up the bot, shared singletons, loads cogs
config.py       - all tunable config (colors, tiers, specs, icon sources)
wcl_client.py   - WarcraftLogs API client
storage.py      - tiny JSON-backed persistence (generic, any cog can use it)
icons.py        - shared icon resolution (guild emoji + auto-provisioned spec emoji)
cogs/
  apply.py      - the whole guild-application feature (self-contained)
```

**Adding a new feature later** (e.g. attendance tracking, announcements):
create a new file in `cogs/`, add one line to `EXTENSIONS` in `bot.py`. You
shouldn't need to touch `cogs/apply.py` at all unless the new feature
actually needs to change how applications work.

## What the bot does

1. `/apply character:<name>` - looks the character up on WarcraftLogs.
2. If found, asks the applicant (via ephemeral dropdowns) for their main
   role, main spec, and any other specs they can play - logs alone can't
   always tell us this reliably (multi-spec players, ambiguous specs like
   Feral).
3. A modal asks for an optional note for the officers.
4. The bot offers to attach an optional gear screenshot - the applicant
   replies with an image in the same channel, or clicks Skip.
5. Posts a summary embed to the review channel (class/spec/role with icons,
   guild, level, tier performance, per-boss breakdown, note, screenshot) with
   checkmark/X reactions.
6. A moderator reacts to approve (assigns the Fresh role + DMs the
   applicant) or deny (DMs the applicant).

## Setup

### 1. Discord bot

Same as before - see the Developer Portal steps for creating the bot,
enabling Server Members Intent, and inviting it with `bot` +
`applications.commands` scopes and Send Messages, Add Reactions,
Manage Roles, Read Message History, Embed Links, View Channels
permissions. Make sure the bot's role sits above the "Fresh" role.

### 2. Class and role emoji (manual, one-time)

Upload emoji to your server named to match config.py's CLASS_EMOJI_NAMES
and ROLE_EMOJI_NAMES (e.g. :Classicons_mage:, :Roleicon_tank:). The bot
looks these up by name at runtime.

### 3. Spec icons (automatic - no manual upload)

Discord embeds can't inline an arbitrary image URL as small text-sized
icons - only real emoji render inline. Rather than you uploading spec
icons by hand, the bot uploads them itself: fill in config.py's
SPEC_ICON_URLS with a stable image URL per class/spec (a Wowhead-hosted
icon URL works well), and on startup the bot downloads each one and
creates a bot-owned "application emoji" from it automatically. This
requires discord.py 2.5+ (already pinned in requirements.txt). If a URL
is missing or fails to download, that spec just falls back to plain text -
it won't crash the bot.

### 4. Everything else

Same .env setup as before (Discord token, WCL credentials, channel/role
IDs, realm slug/region). Then:

```
pip install -r requirements.txt
python bot.py
```

## Notes

- Gear display was removed - the WCL query needed for reliable gear data
  didn't pan out; the optional screenshot step covers this instead.
- Previous-tier zone IDs are still best-effort. PREVIOUS_TIER's zone id in
  config.py hasn't been independently verified the way CURRENT_TIER's was
  (via debug_rankings.py against a real character) - if the fallback ever
  silently shows nothing, that's why. Same fix as before: run
  debug_rankings.py against a character with logs in that tier and confirm
  the zone id.
- Persistence: applications.json still holds pending applications (now
  including spec/role/note) so a restart doesn't lose them.
