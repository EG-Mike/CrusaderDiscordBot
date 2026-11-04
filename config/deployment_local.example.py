"""
Example per-machine overrides. Copy to config/deployment_local.py (which
git ignores) and fill in. Anything set here replaces the same-named value
from config/deployment.py on THIS machine only - see config/overrides.py.

Typical use: a test bot in a test Discord server, which needs that server's
role/channel/tag IDs instead of the live server's ones hardcoded in
deployment.py. Delete any line you don't need.
"""

# Turn the web dashboard on for this machine.
FEATURE_WEB_DASHBOARD_ENABLED = True

# --- Test-server IDs (right-click -> Copy ID with Developer Mode on) ---
APPROVER_ROLE_ID = 0
REGULAR_ROLE_ID = 0
ORGANIZER_ROLE_ID = 0
ARCHIVE_CHANNEL_ID = 0

FRESH_EXEMPT_ROLE_IDS = {
    # 123456789012345678: "Fresh",
    # 123456789012345678: "Regular",
    # 123456789012345678: "Organizer",
}

AUTO_ROLE_IDS = {
    # "Tank": 123456789012345678,
    # "Healer": 123456789012345678,
    # "DPS": 123456789012345678,
    # "Mage": 123456789012345678, ... one per class
}

# Forum tag IDs of the test server's raid-summary forum.
TIER_TAG_IDS = {
    # "BT/Hyjal": 123456789012345678,
}
RAID_TYPE_TAG_IDS = {
    # "main": 123456789012345678,
    # "alt": 123456789012345678,
}
