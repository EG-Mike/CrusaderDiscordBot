"""
Dashboard API for auction prices (auction_prices.py, cogs/auction_prices.py).

  GET  /api/auction                 which house, the snapshot's age, the guild chat channel, problems
  GET  /api/auction/search?q=...    items matching the text with their price, best match first
  GET  /api/auction/houses?ruleset_id=1  the rulesets and that ruleset's auction houses
  POST /api/auction/house           {"ruleset_id", "house_id"}: use this house (saved as settings)
  POST /api/auction/compare         {"ruleset_id", "house_ids": [...], "item"}: how much of an item each
                                    house has for sale, to find out which one is the guild's
  POST /api/auction/refresh         read the house's latest snapshot now

Moderators only. Picking the house and refreshing are audited (the house
also shows in the settings history).
"""

import asyncio

import auction_prices
import config
import gear
import wow_api
from webapi import settings as settings_api
from webapi.common import BOT_KEY, BadRequest, actor_label, audit, error, forbidden_unless, json_response, \
    read_json

LEVEL = "moderator"
MAX_COMPARE_HOUSES = 15


def _guard(request):
    denied = forbidden_unless(request, LEVEL, "auction prices")
    if denied:
        return denied, None
    cog = request.app[BOT_KEY].get_cog("AuctionPricesCog")
    if cog is None:
        return error("Auction prices are switched off (Settings, Features).", 409), None
    if getattr(request.app[BOT_KEY], "blizzard", None) is None:
        return error("Blizzard API keys aren't set up (Settings, General, Integrations).", 409), None
    return None, cog


def _item(item: dict) -> dict:
    return {**item, "url": gear.item_url(item["id"])}


async def overview(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    house_name = None
    if config.AUCTION_HOUSE_ID:
        try:
            houses = await auction_prices.houses(request.app[BOT_KEY], int(config.AUCTION_RULESET_ID))
            house_name = next((h["name"] for h in houses if h["id"] == int(config.AUCTION_HOUSE_ID)), None)
        except auction_prices.AuctionError:
            pass
    snapshot = await asyncio.to_thread(auction_prices.snapshot)
    if snapshot:
        snapshot.pop("modified", None)
    channel = request.app[BOT_KEY].get_channel(int(config.AUCTION_PRICE_CHANNEL_ID or 0))
    return json_response({
        "ruleset_id": int(config.AUCTION_RULESET_ID or 0),
        "house_id": int(config.AUCTION_HOUSE_ID or 0) or None,
        "house_name": house_name,
        "snapshot": snapshot,
        "problem": cog.problem() or cog.last_problem,
        "chat_channel": {"id": str(channel.id), "name": channel.name} if channel else None,
        "triggers": list(config.AUCTION_PRICE_TRIGGERS or []),
    })


async def search(request):
    denied, _cog = _guard(request)
    if denied:
        return denied
    text = (request.query.get("q") or "").strip()[:80]
    if len(text) < 2:
        return json_response({"items": []})
    try:
        items = await auction_prices.lookup(request.app[BOT_KEY], text)
    except auction_prices.AuctionError as exc:
        return error(str(exc), 502)
    return json_response({"items": [_item(i) for i in items]})


def _int(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BadRequest(f"Unknown {what}.")
    return value


async def list_houses(request):
    denied, _cog = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    try:
        ruleset_id = int(request.query.get("ruleset_id") or config.AUCTION_RULESET_ID or 1)
        rulesets = (await wow_api.reference(bot)).get("rulesets", [])
        houses = await auction_prices.houses(bot, ruleset_id)
    except ValueError:
        return error("Unknown ruleset.", 400)
    except (auction_prices.AuctionError, wow_api.WowApiError) as exc:
        return error(str(exc), 502)
    return json_response({"ruleset_id": ruleset_id, "rulesets": rulesets, "houses": houses})


async def set_house(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        body = await read_json(request)
        ruleset_id, house_id = _int(body.get("ruleset_id"), "ruleset"), _int(body.get("house_id"), "auction house")
        houses = await auction_prices.houses(request.app[BOT_KEY], ruleset_id)
        house = next((h for h in houses if h["id"] == house_id), None)
        if house is None:
            raise BadRequest("That ruleset has no such auction house.")
        for key, value in (("AUCTION_RULESET_ID", ruleset_id), ("AUCTION_HOUSE_ID", house_id)):
            settings_api.save(key, settings_api.validate(key, value), actor_label(request))
    except (BadRequest, settings_api.SettingError) as exc:
        return error(str(exc), 400)
    except auction_prices.AuctionError as exc:
        return error(str(exc), 502)
    audit(request, "auction", "auction.house", f"Picked the auction house {house['name']} ({house_id})",
          details={"ruleset_id": ruleset_id, "house_id": house_id})
    cog.refresh_soon()
    return json_response({"ok": True})


async def compare(request):
    denied, _cog = _guard(request)
    if denied:
        return denied
    bot = request.app[BOT_KEY]
    try:
        body = await read_json(request)
        ruleset_id = _int(body.get("ruleset_id"), "ruleset")
        house_ids = body.get("house_ids")
        if (not isinstance(house_ids, list) or not house_ids or len(house_ids) > MAX_COMPARE_HOUSES
                or not all(isinstance(h, int) and not isinstance(h, bool) for h in house_ids)):
            raise BadRequest(f"Pick 1 to {MAX_COMPARE_HOUSES} auction houses.")
        known = {h["id"] for h in await auction_prices.houses(bot, ruleset_id)}
        if not set(house_ids) <= known:
            raise BadRequest("That ruleset has no such auction house.")
        name = body.get("item")
        if not isinstance(name, str) or not name.strip():
            raise BadRequest("Which item? E.g. Linen Cloth.")
        item = await auction_prices.exact(bot, name.strip()[:80])
        if item is None:
            raise BadRequest(f"No item is called {name.strip()[:80]}.")
        rows = await auction_prices.compare(bot, ruleset_id, list(dict.fromkeys(house_ids)), item["id"])
    except BadRequest as exc:
        return error(str(exc), 400)
    except auction_prices.AuctionError as exc:
        return error(str(exc), 502)
    return json_response({"item": _item({k: item[k] for k in ("id", "name", "quality")}), "houses": rows})


async def refresh(request):
    denied, cog = _guard(request)
    if denied:
        return denied
    try:
        result = await cog.refresh(force=True)
    except auction_prices.AuctionError as exc:
        return error(str(exc), 409)
    audit(request, "auction", "auction.refresh", "Read the auction house prices again",
          details={"items": result["items"], "snapshot_at": result["snapshot_at"]})
    return json_response(result)


def register(app):
    app.router.add_get("/api/auction", overview)
    app.router.add_get("/api/auction/search", search)
    app.router.add_get("/api/auction/houses", list_houses)
    app.router.add_post("/api/auction/house", set_house)
    app.router.add_post("/api/auction/compare", compare)
    app.router.add_post("/api/auction/refresh", refresh)
