"""
Config package - split into two files:

  - game_data.py    static TBC World of Warcraft reference data (class
                     colors, talent trees, WCL/item-quality color scales)
                     that's true for every deployment and never needs
                     touching per-guild.
  - deployment.py    everything actually tuned per-deployment: logging,
                     feature flags, Discord role/channel/tag IDs, icon
                     sourcing, attendance/raid-log tuning knobs, tracked
                     abilities, and the current/previous raid tier.

Both are re-exported here so every existing `config.SOMETHING` reference
elsewhere in the codebase keeps working unchanged - this package IS
`config` as far as any importer is concerned; the split is purely about
keeping the two kinds of constants easy to tell apart on disk.
"""

# .env is loaded here (not only in bot.py) because deployment.py uses some
# of its values as defaults - see its "Discord server" section.
from dotenv import load_dotenv as _load_dotenv

_load_dotenv()

from .game_data import *  # noqa: F401,F403,E402
from .deployment import *  # noqa: F401,F403

# Optional overrides on top of deployment.py: config/deployment_local.py
# (per-machine, gitignored) and values saved in the web dashboard. See
# config/overrides.py. A no-op on a deployment that uses neither.
from . import overrides as _overrides  # noqa: E402

_overrides.apply_all(__import__(__name__))
