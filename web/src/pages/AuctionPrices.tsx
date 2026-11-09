// Auction prices (webapi/auction_api.py, auction_prices.py): search an item
// and see what it costs on the guild's auction house, and how old Blizzard's
// data is. Also where moderators pick the guild's auction house: every
// ruleset has several houses per faction with the same name, so the page can
// compare how much of an item each has for sale (match it with the game).
// The search text is in the address (/auction-prices?q=...).
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, Coins, FlaskConical, MessageSquare, RefreshCw, Scale, Search, Settings2, Store } from "lucide-react";
import {
  ApiError,
  api,
  type AuctionComparison,
  type AuctionHouses,
  type AuctionItem,
  type AuctionOverview,
  type AuctionTry,
} from "../lib/api";
import { fullDateTime, useNow } from "../lib/time";
import { QUALITY_COLORS } from "../lib/wow";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Skeleton, cx } from "../components/ui";

/** The game's coin colours. */
const COIN_COLORS = { g: "#ffd700", s: "#c7c7cf", c: "#eda55f" } as const;

/** 43322 copper -> 4g 33s 22c, the letters in the coin colours; units that are 0 are left out. */
export function Money({ copper, className }: { copper: number | null | undefined; className?: string }) {
  const value = Math.round(copper ?? 0);
  const parts = (
    [
      [Math.floor(value / 10000), "g"],
      [Math.floor(value / 100) % 100, "s"],
      [value % 100, "c"],
    ] as const
  ).filter(([amount]) => amount > 0);
  const shown = parts.length ? parts : ([[0, "c"]] as const);
  return (
    <span className={cx("whitespace-nowrap font-semibold tabular-nums", className)}>
      {shown.map(([amount, unit], i) => (
        <span key={unit}>
          {i > 0 && " "}
          {amount}
          <span style={{ color: COIN_COLORS[unit] }}>{unit}</span>
        </span>
      ))}
    </span>
  );
}

/** "just now", "12mins ago", "3hrs ago", "2 days ago": the same words as the bot's answers. */
export function ageText(iso: string | null | undefined, now = Date.now()) {
  if (!iso) return "unknown age";
  const seconds = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (seconds < 90) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}min${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}hr${hours === 1 ? "" : "s"} ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

function ItemName({ item }: { item: AuctionItem }) {
  const style = { color: QUALITY_COLORS[item.quality ?? ""] };
  return item.url ? (
    <a href={item.url} target="_blank" rel="noreferrer" className="font-medium hover:underline" style={style}>
      [{item.name}]
    </a>
  ) : (
    <span className="font-medium" style={style}>
      [{item.name}]
    </span>
  );
}

function PriceRow({ item, age }: { item: AuctionItem; age: string }) {
  const price = item.price;
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-4 py-2 md:px-5">
      <ItemName item={item} />
      {price ? (
        <>
          <span className="text-faint">-</span>
          <Money copper={price.min_price} />
          <span className="text-muted">({age})</span>
          <span className="w-full text-2xs text-faint sm:ml-auto sm:w-auto">
            {price.quantity.toLocaleString()} for sale in {price.listings.toLocaleString()} auction{price.listings === 1 ? "" : "s"}
          </span>
        </>
      ) : (
        <span className="text-2xs text-faint">not on the auction house ({age})</span>
      )}
    </li>
  );
}

function HousePicker({ current, onPicked }: { current: AuctionOverview; onPicked: () => void }) {
  const toast = useToast();
  const [rulesetId, setRulesetId] = useState(current.ruleset_id || 1);
  const [data, setData] = useState<AuctionHouses | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [group, setGroup] = useState<string>("");
  const [item, setItem] = useState("Linen Cloth");
  const [comparison, setComparison] = useState<AuctionComparison | null>(null);
  const [busy, setBusy] = useState<"compare" | number | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setComparison(null);
    api
      .get<AuctionHouses>(`/api/auction/houses?ruleset_id=${rulesetId}`)
      .then((r) => {
        setData(r);
        const mine = r.houses.find((h) => h.id === current.house_id);
        setGroup(mine?.name ?? r.houses[0]?.name ?? "");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the auction houses"));
  }, [rulesetId, current.house_id]);

  const groups = useMemo(() => [...new Set((data?.houses ?? []).map((h) => h.name))], [data]);
  const inGroup = (data?.houses ?? []).filter((h) => h.name === group);

  const compare = async (e: FormEvent) => {
    e.preventDefault();
    setBusy("compare");
    setComparison(null);
    try {
      setComparison(
        await api.post<AuctionComparison>("/api/auction/compare", {
          ruleset_id: rulesetId,
          house_ids: inGroup.map((h) => h.id),
          item: item.trim(),
        }),
      );
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "The comparison didn't work");
    } finally {
      setBusy(null);
    }
  };

  const pick = async (houseId: number) => {
    setBusy(houseId);
    try {
      await api.post("/api/auction/house", { ruleset_id: rulesetId, house_id: houseId });
      toast("ok", "Auction house saved. Reading its prices now.");
      onPicked();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Couldn't save");
    } finally {
      setBusy(null);
    }
  };

  const results = new Map((comparison?.houses ?? []).map((r) => [r.house_id, r]));

  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Your auction house</div>
        <p className="mt-0.5 text-2xs text-faint">
          Each ruleset has several auction houses per faction, all with the same name. To find yours, search an item in
          the game (a common material works best), note how many are for sale, then compare here and pick the house
          with about the same number.
        </p>
      </header>
      {error ? (
        <p className="px-4 py-3 text-crimson md:px-5">{error}</p>
      ) : !data ? (
        <Skeleton className="m-4 h-24" />
      ) : (
        <div className="space-y-3 px-4 py-3 md:px-5">
          <div className="flex flex-wrap items-center gap-2">
            <select className="input h-9 w-auto py-1 text-xs" value={rulesetId} onChange={(e) => setRulesetId(Number(e.target.value))} aria-label="Ruleset">
              {(data.rulesets.length ? data.rulesets : [{ id: rulesetId, name: `Ruleset ${rulesetId}` }]).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
            <select className="input h-9 w-auto py-1 text-xs" value={group} onChange={(e) => (setGroup(e.target.value), setComparison(null))} aria-label="Faction">
              {groups.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          </div>
          <form onSubmit={compare} className="flex flex-wrap items-center gap-2">
            <input className="input h-9 min-w-[10rem] flex-1" value={item} onChange={(e) => setItem(e.target.value)} placeholder="An item, e.g. Linen Cloth" aria-label="Item to compare" />
            <Button type="submit" icon={<Scale className="h-3.5 w-3.5" />} loading={busy === "compare"} disabled={!item.trim() || !inGroup.length}>
              Compare
            </Button>
          </form>
          {busy === "compare" && <p className="text-2xs text-faint">Reading {inGroup.length} auction houses, this takes a few seconds...</p>}
          <ul className="divide-y divide-line rounded-lg border border-line">
            {inGroup.map((h) => {
              const row = results.get(h.id);
              const mine = current.house_id === h.id && current.ruleset_id === rulesetId;
              return (
                <li key={h.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                  <span className="font-medium">
                    {h.name} <span className="text-2xs text-faint">#{h.id}</span>
                  </span>
                  {row && (
                    <span className="text-xs text-muted">
                      {row.quantity ? (
                        <>
                          {row.quantity.toLocaleString()} for sale from <Money copper={row.min_price} />
                        </>
                      ) : (
                        "none for sale"
                      )}
                    </span>
                  )}
                  <span className="ml-auto">
                    {mine ? (
                      <Badge tone="gold">
                        <Check className="h-2.5 w-2.5" /> Yours
                      </Badge>
                    ) : (
                      <Button size="sm" loading={busy === h.id} disabled={busy !== null} onClick={() => pick(h.id)}>
                        Use this one
                      </Button>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
          {comparison && (
            <p className="text-2xs text-faint">
              Compared <ItemName item={comparison.item} />. The numbers are from Blizzard's latest data, so they can be a little off from the game.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

/** The phrases the bot listens for in the guild chat, and a dry run of its answer to a message. */
function ChatAnswers({ overview }: { overview: AuctionOverview }) {
  const [message, setMessage] = useState("price [Linen Cloth]");
  const [result, setResult] = useState<AuctionTry | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tryIt = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await api.post<AuctionTry>("/api/auction/try", { message }));
    } catch (err) {
      setResult(null);
      setError(err instanceof ApiError ? err.message : "That didn't work");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-line px-4 py-3 md:px-5">
        <div>
          <div className="label">Guild chat answers</div>
          <p className="mt-0.5 text-2xs text-faint">
            The bot answers when one of these comes right before a linked [Item]
            {overview.trigger_after_item ? ", or ends the message right after one (\"[Iron Bar] price?\")" : ""}. Case doesn't matter.
          </p>
        </div>
        <Link to="/settings?group=auction#AUCTION_PRICE_TRIGGERS" className="inline-flex items-center gap-1 text-2xs text-gold hover:underline">
          <Settings2 className="h-3 w-3" /> Edit
        </Link>
      </header>
      <div className="space-y-3 px-4 py-3 md:px-5">
        <div className="flex flex-wrap gap-1.5">
          {overview.triggers.length ? (
            overview.triggers.map((t) => (
              <span key={t} className="rounded-md border border-line bg-raised/40 px-2 py-0.5 text-xs">
                {t}
              </span>
            ))
          ) : (
            <span className="text-faint">No phrases: the bot never answers.</span>
          )}
        </div>
        <form onSubmit={tryIt} className="flex flex-wrap items-center gap-2">
          <input className="input h-9 min-w-[12rem] flex-1" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="A guild chat message, e.g. price [Linen Cloth]" aria-label="Message to try" />
          <Button type="submit" icon={<FlaskConical className="h-3.5 w-3.5" />} loading={busy} disabled={!message.trim()}>
            Try it
          </Button>
        </form>
        {error && <p className="text-xs text-crimson">{error}</p>}
        {result && (
          <div className="rounded-lg bg-raised/40 px-3 py-2 text-xs">
            {!result.items.length ? (
              <span className="text-muted">The bot wouldn't answer this: no phrase right next to a linked [Item].</span>
            ) : result.answer ? (
              <>
                <div className="mb-1 text-2xs text-faint">
                  The bot would answer{overview.answer_in === "dm" ? " by DM to the player who asked" : " in the channel"} (nothing was sent):
                </div>
                <div className="whitespace-pre-line">{result.answer}</div>
              </>
            ) : (
              <span className="text-warn">
                It would ask about {result.items.map((i) => `[${i}]`).join(" ")}, but can't answer yet{result.problem ? `: ${result.problem}` : " (no auction house data yet)"}.
              </span>
            )}
          </div>
        )}
        <p className="text-2xs text-faint">
          Messages from players who haven't linked their Discord account reach the channel without their text, so the bot can't answer those.
        </p>
      </div>
    </section>
  );
}

export default function AuctionPricesPage() {
  const toast = useToast();
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const [overview, setOverview] = useState<AuctionOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<AuctionItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [showPicker, setShowPicker] = useState(false);

  const load = useCallback(() => {
    api
      .get<AuctionOverview>("/api/auction")
      .then((r) => {
        setOverview(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the auction house"));
  }, []);
  useEffect(load, [load]);

  // Search as you type (debounced); the text goes in the address.
  useEffect(() => {
    const id = window.setTimeout(() => {
      if (draft.trim() !== q) setParams(draft.trim() ? { q: draft.trim() } : {}, { replace: true });
    }, 350);
    return () => window.clearTimeout(id);
  }, [draft, q, setParams]);

  useEffect(() => {
    if (q.length < 2) {
      setItems(null);
      return;
    }
    let cancelled = false;
    setSearching(true);
    api
      .get<{ items: AuctionItem[] }>(`/api/auction/search?q=${encodeURIComponent(q)}`)
      .then((r) => !cancelled && setItems(r.items))
      .catch((e) => !cancelled && toast("error", e instanceof ApiError ? e.message : "The search didn't work"))
      .finally(() => !cancelled && setSearching(false));
    return () => {
      cancelled = true;
    };
  }, [q, toast, overview?.snapshot?.snapshot_at]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      await api.post("/api/auction/refresh");
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't read the auction house");
    } finally {
      setRefreshing(false);
    }
  };

  const picked = () => {
    setShowPicker(false);
    load();
    // The bot reads the new house's prices in the background; look again shortly.
    window.setTimeout(load, 6000);
  };

  const snapshot = overview?.snapshot;
  const age = ageText(snapshot?.snapshot_at, now);

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Auction prices"
        description="What an item costs on your auction house: the lowest buyout price for one, from Blizzard's latest data (renewed about every hour)."
        actions={
          overview?.house_id ? (
            <Button icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={refresh} loading={refreshing}>
              Check for new prices
            </Button>
          ) : undefined
        }
      />

      {error ? (
        <div className="card">
          <EmptyState icon={<Coins className="h-6 w-6" />} title="Auction prices aren't available">
            {error}
          </EmptyState>
        </div>
      ) : !overview ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="space-y-5">
          <div className="card flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 text-xs md:px-5">
            <span className="inline-flex items-center gap-1.5">
              <Store className="h-3.5 w-3.5 text-faint" />
              {overview.house_id ? (
                <>
                  {overview.house_name ?? "Auction house"} <span className="text-faint">#{overview.house_id}</span>
                </>
              ) : (
                <span className="text-warn">No auction house picked yet</span>
              )}
              {overview.house_id && (
                <button type="button" className="ml-1 text-gold hover:underline" onClick={() => setShowPicker((v) => !v)}>
                  {showPicker ? "Close" : "Change"}
                </button>
              )}
            </span>
            {snapshot?.snapshot_at && (
              <span className="text-muted" title={fullDateTime(snapshot.snapshot_at)}>
                Data from {age} · {snapshot.items.toLocaleString()} items, {snapshot.auctions.toLocaleString()} auctions
              </span>
            )}
            <span className="inline-flex items-center gap-1.5 text-muted">
              <MessageSquare className="h-3.5 w-3.5 text-faint" />
              {overview.chat_channel ? (
                <>
                  Answers "price [Item]" in #{overview.chat_channel.name}
                  {overview.answer_in === "dm" ? " by DM to the player who asked" : ""}
                </>
              ) : (
                <>
                  Not answering price questions in Discord.{" "}
                  <Link to="/settings?group=auction#AUCTION_PRICE_CHANNEL_ID" className="text-gold hover:underline">
                    Pick the guild chat channel
                  </Link>
                </>
              )}
            </span>
            {overview.problem && overview.house_id && <span className="text-crimson">{overview.problem}</span>}
          </div>

          {(!overview.house_id || showPicker) && <HousePicker current={overview} onPicked={picked} />}

          {overview.house_id && (
            <section className="card">
              <div className="border-b border-line px-4 py-3 md:px-5">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
                  <input className="input h-10 pl-9" autoFocus placeholder="Search an item, e.g. Linen Cloth" value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="Search items" />
                </div>
              </div>
              {!snapshot ? (
                <p className="px-4 py-3 text-faint md:px-5">The bot hasn't read this auction house yet. Try "Check for new prices".</p>
              ) : q.length < 2 ? (
                <p className="px-4 py-3 text-faint md:px-5">Type at least two letters. Whole words match best; an unfinished last word works too.</p>
              ) : searching && !items ? (
                <Skeleton className="m-4 h-24" />
              ) : !items?.length ? (
                <p className="px-4 py-3 text-faint md:px-5">No item has that name.</p>
              ) : (
                <ul className={cx("divide-y divide-line", searching && "opacity-60")}>
                  {items.map((item) => (
                    <PriceRow key={item.id} item={item} age={age} />
                  ))}
                </ul>
              )}
            </section>
          )}

          <ChatAnswers overview={overview} />
        </div>
      )}
    </>
  );
}
