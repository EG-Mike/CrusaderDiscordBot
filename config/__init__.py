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

from .game_data import *  # noqa: F401,F403
from .deployment import *  # noqa: F401,F403
