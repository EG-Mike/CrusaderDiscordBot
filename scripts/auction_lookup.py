"""
Looks up live auction house data for WoW Forever from Blizzard's API, without
printing any secret values.

    python scripts/auction_lookup.py "Linen Cloth" 2592
    python scripts/auction_lookup.py --faction horde "Linen Cloth" "Peacebloom" "Copper Bar"
    python scripts/auction_lookup.py --faction horde --fingerprint

Items are names (looked up with Blizzard's item search) or item ids. For each
auction house of the ruleset it prints, per item: the number of listings, how
many are for sale, the cheapest and the median price each. With
--fingerprint it prints each house's most expensive listings instead, which
a player can look for in game to find out which house is theirs.

Forever has rulesets instead of realms (docs/wow-forever-api.md): auction
houses hang off a ruleset (1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP),
and every ruleset has several houses per faction with the same name
("Horde Auction House"). Nothing in the API says which players use which
house. The data is a snapshot Blizzard refreshes about hourly (see the
Last-Modified line). Credentials come from BLIZZARD_CLIENT_ID and
BLIZZARD_CLIENT_SECRET (environment or .env); HTTPS_PROXY is honoured.
"""

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
from typing import Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blizzard_client import BlizzardClient  # noqa: E402


def money(copper: float) -> str:
    copper = int(round(copper))
    gold, silver, rest = copper // 10000, copper // 100 % 100, copper % 100
    return " ".join(part for part in (f"{gold}g" if gold else "", f"{silver}s" if gold or silver else "", f"{rest}c") if part)


async def resolve_item(client: BlizzardClient, session, region: str, text: str) -> Optional[dict]:
    """An item id, or the best name match from Blizzard's item search."""
    if text.isdigit():
        body = await client.get_game_data(f"item/{text}", "static-forever", session=session)
        return {"id": int(text), "name": (body or {}).get("name") or f"item {text}"}
    status, _headers, body = await client.request("/data/wow/search/item", f"static-forever-{region}",
                                                  {"name.en_US": text, "_pageSize": 50}, session=session)
    results = [r.get("data") or {} for r in (body or {}).get("results") or []] if status == 200 else []
    if not results:
        return None
    needle = text.lower()
    best = min(results, key=lambda d: (((d.get("name") or {}).get("en_US") or "").lower() != needle, d.get("id", 0)))
    return {"id": best["id"], "name": (best.get("name") or {}).get("en_US") or text}


async def item_names(client: BlizzardClient, session, ids) -> Dict[int, str]:
    async def one(item_id):
        try:
            body = await client.get_game_data(f"item/{item_id}", "static-forever", session=session)
        except aiohttp.ClientError:
            body = None
        return item_id, (body or {}).get("name") or f"item {item_id}"
    return dict(await asyncio.gather(*(one(i) for i in set(ids))))


def unit_price(auction: dict) -> float:
    """Copper per item: commodities have unit_price, other auctions a buyout for the whole stack."""
    if auction.get("unit_price"):
        return auction["unit_price"]
    return (auction.get("buyout") or 0) / max(1, auction.get("quantity") or 1)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("items", nargs="*", help="item names or ids")
    parser.add_argument("--region", default="eu")
    parser.add_argument("--ruleset", type=int, default=1, help="1 PvP (default), 2 Normal, 3 Roleplay, 15 Roleplay PvP")
    parser.add_argument("--faction", help="only houses whose name contains this, e.g. horde, alliance, blackwater")
    parser.add_argument("--fingerprint", action="store_true", help="list each house's most expensive listings")
    parser.add_argument("--top", type=int, default=5, help="listings per house with --fingerprint")
    args = parser.parse_args()
    if not args.items and not args.fingerprint:
        parser.error("give item names/ids, or --fingerprint")

    load_dotenv()
    client_id, secret = os.getenv("BLIZZARD_CLIENT_ID"), os.getenv("BLIZZARD_CLIENT_SECRET")
    if not client_id or not secret:
        print("BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET are not set.")
        return 1
    client = BlizzardClient(client_id, secret, args.region, 
                            cache_path=os.path.join(tempfile.mkdtemp(), "items.json"))  # no cache needed
    dynamic = f"dynamic-forever-{args.region}"

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as session:
        status, _h, index = await client.request(f"/data/wow/ruleset/{args.ruleset}/auctions/index", dynamic,
                                                 session=session)
        if status != 200:
            print(f"The auction house list answered HTTP {status}.")
            return 1
        houses = [h for h in index.get("auctions", [])
                  if not args.faction or args.faction.lower() in (h.get("name") or "").lower()]
        print(f"Ruleset {args.ruleset} ({args.region}): {len(houses)} house(s): "
              + ", ".join(f"{h['id']} {h.get('name')}" for h in houses))

        items: List[dict] = []
        for text in args.items:
            item = await resolve_item(client, session, args.region, text)
            if item is None:
                print(f"No item found for {text!r}.")
            else:
                items.append(item)

        for house in houses:
            base = f"/data/wow/ruleset/{args.ruleset}/auctions/{house['id']}"
            (cs, ch, commodities), (ns, _nh, others) = await asyncio.gather(
                client.request(f"{base}/commodities", dynamic, session=session),
                client.request(base, dynamic, session=session))
            auctions = (commodities or {}).get("auctions", []) + (others or {}).get("auctions", [])
            print(f"\n== House {house['id']} {house.get('name')}: {len(auctions)} auctions "
                  f"(commodities HTTP {cs}, items HTTP {ns}), snapshot {ch.get('Last-Modified', '?')}")
            for item in items:
                listed = [a for a in auctions if (a.get("item") or {}).get("id") == item["id"]]
                if not listed:
                    print(f"  {item['name']} ({item['id']}): none listed")
                    continue
                prices = sorted(unit_price(a) for a in listed)
                print(f"  {item['name']} ({item['id']}): {len(listed)} listings, "
                      f"{sum(a.get('quantity') or 1 for a in listed)} for sale, cheapest {money(prices[0])}, "
                      f"median {money(statistics.median(prices))} each")
            if args.fingerprint:
                top = sorted(auctions, key=lambda a: a.get("buyout") or (a.get("unit_price") or 0) * (a.get("quantity") or 1),
                             reverse=True)[:args.top]
                names = await item_names(client, session, [a["item"]["id"] for a in top])
                for a in top:
                    total = a.get("buyout") or (a.get("unit_price") or 0) * (a.get("quantity") or 1)
                    print(f"  {names[a['item']['id']]} ({a['item']['id']}) x{a.get('quantity') or 1}: "
                          f"{money(total)} total, {money(unit_price(a))} each, {a.get('time_left')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
