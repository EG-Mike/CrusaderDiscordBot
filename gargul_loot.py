"""
Parser for Gargul (WoW loot-tracking addon) CSV exports.

Verified against a real export (2026-08) - the export is a plain CSV with a
header row: dateTime,character,itemID,offspec,id

Notably, it does NOT include the item name, icon, or which boss it dropped
from - just the winner and itemID. Item name/icon/link are resolved
separately via wowhead.py (by itemID, which is stable and always accurate -
better than hardcoding a name/icon table here). Boss attribution isn't
attempted at all: WCL's public API doesn't expose a reliable per-item loot
event, and guessing item->boss from a hardcoded tier loot table risks
silently wrong data - so the raid-summary loot section is a single
chronological list (row order = award order, since that's the order Gargul
wrote them in) rather than grouped by boss.

If Gargul's export format ever changes (new column, renamed column), this
is the one place that needs updating.
"""

import csv
import io

REQUIRED_COLUMNS = {"dateTime", "character", "itemID", "offspec"}


class GargulParseError(ValueError):
    pass


def parse_gargul_export(text: str) -> list[dict]:
    """
    Returns a list of dicts, one per loot row, in original (award) order:
      {"date": str, "character": str, "item_id": int, "offspec": bool, "gargul_id": str}

    Raises GargulParseError with a human-readable message on a header/format
    mismatch, so the caller can surface it to the mod instead of a raw
    traceback.
    """
    text = text.strip()
    if not text:
        raise GargulParseError("The loot export is empty.")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise GargulParseError("Couldn't find a header row in the loot export.")

    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise GargulParseError(
            f"Loot export is missing expected column(s): {', '.join(sorted(missing))}. "
            f"Found columns: {', '.join(reader.fieldnames)}. If Gargul's export format "
            "changed, gargul_loot.py needs updating to match."
        )

    rows = []
    for i, raw in enumerate(reader, start=2):  # start=2: header is line 1
        character = (raw.get("character") or "").strip()
        item_id_raw = (raw.get("itemID") or "").strip()
        if not character or not item_id_raw:
            continue  # skip blank/malformed rows rather than failing the whole import

        try:
            item_id = int(item_id_raw)
        except ValueError:
            raise GargulParseError(f"Line {i}: itemID '{item_id_raw}' isn't a number.")

        rows.append({
            "date": (raw.get("dateTime") or "").strip(),
            "character": character,
            "item_id": item_id,
            "offspec": (raw.get("offspec") or "").strip() == "1",
            "gargul_id": (raw.get("id") or "").strip(),
        })

    if not rows:
        raise GargulParseError("No loot rows found in the export.")

    return rows
