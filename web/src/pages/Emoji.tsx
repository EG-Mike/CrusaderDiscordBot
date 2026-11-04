import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Check, ClipboardCopy, ImagePlus, Layers, Loader2, Plus, Recycle, Search, Smile, Sparkles, Trash2 } from "lucide-react";
import { ApiError, api, type EmojiAddResult, type EmojiOverview, type EmojiSearchResult, type LibraryEmoji, type PlainEmoji } from "../lib/api";
import { loadCustomEmoji } from "../lib/emoji";
import { useLive } from "../lib/live";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Skeleton, StatTile, cx } from "../components/ui";

const kindLabel = (kind: string) => (kind === "item" ? "Item" : "Spell");

function CopyText({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title={`Copy ${text}`}
      aria-label={label}
      onClick={() =>
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        })
      }
      className="rounded p-1 text-faint transition hover:bg-raised hover:text-fg"
    >
      {copied ? <Check className="h-3.5 w-3.5 text-ok" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
    </button>
  );
}

function ResultRow({ r, onAdd, busy, canManage }: { r: EmojiSearchResult; onAdd: (r: EmojiSearchResult) => void; busy: boolean; canManage: boolean }) {
  if (r.error)
    return (
      <li className="flex items-center gap-3 px-4 py-2.5 md:px-5">
        <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
        <span className="text-muted">
          {kindLabel(r.kind)} {r.game_id}: {r.error}
        </span>
      </li>
    );
  const others = r.emoji?.aliases.filter((a) => !(a.kind === r.kind && a.game_id === r.game_id)) ?? [];
  return (
    <li className="flex items-center gap-3 px-4 py-2.5 md:px-5">
      {r.image_url ? <img src={r.image_url} alt="" className="h-9 w-9 shrink-0 rounded ring-1 ring-line" loading="lazy" /> : <div className="h-9 w-9 shrink-0 rounded bg-raised" />}
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-medium">{r.name}</span>
          <Badge tone={r.kind === "item" ? "gold" : "info"}>{kindLabel(r.kind)}</Badge>
          <span className="hidden font-mono text-2xs text-faint sm:inline">#{r.game_id}</span>
        </div>
        <div className="mt-0.5 truncate text-2xs text-faint">
          {r.state === "added" && <>Already has the emoji :{r.emoji?.name}:</>}
          {r.state === "shared" && (
            <>
              Same icon as {others.slice(0, 2).map((a) => a.name).join(", ") || "another item"}: reuses :{r.emoji?.name}:
            </>
          )}
          {r.state === "new" && <>New icon{r.icon_name ? ` (${r.icon_name})` : r.file_id ? ` (file ${r.file_id})` : ""}</>}
          {r.source === "wowhead" && " · found on Wowhead (not on Blizzard)"}
        </div>
      </div>
      {r.state === "added" ? (
        <span className="inline-flex shrink-0 items-center gap-1 text-xs text-ok">
          <Check className="h-3.5 w-3.5" /> Added
        </span>
      ) : (
        <Button
          size="sm"
          variant={r.state === "shared" ? "outline" : "primary"}
          icon={r.state === "shared" ? <Recycle className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          onClick={() => onAdd(r)}
          loading={busy}
          disabled={!canManage}
          className="shrink-0"
        >
          {r.state === "shared" ? "Reuse" : "Add"}
        </Button>
      )}
    </li>
  );
}

function LibraryCard({ e, onDelete, canManage }: { e: LibraryEmoji; onDelete: (e: LibraryEmoji) => void; canManage: boolean }) {
  return (
    <li className="flex gap-3 rounded-lg border border-line bg-bg/40 p-3">
      <img src={e.url} alt="" className="h-10 w-10 shrink-0 rounded" loading="lazy" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1">
          <span className="truncate font-mono text-xs text-fg">:{e.name}:</span>
          <CopyText text={e.text} label={`Copy :${e.name}:`} />
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          {e.aliases.length ? (
            e.aliases.map((a) => (
              <span key={`${a.kind}${a.game_id}`} className="rounded border border-line bg-surface px-1.5 py-px text-2xs text-muted" title={`${kindLabel(a.kind)} ${a.game_id}`}>
                {a.name}
              </span>
            ))
          ) : (
            <span className="text-2xs text-faint">No items or spells yet</span>
          )}
        </div>
      </div>
      {canManage && (
        <button type="button" onClick={() => onDelete(e)} aria-label={`Delete :${e.name}:`} className="self-start rounded p-1 text-faint transition hover:bg-crimson/10 hover:text-crimson">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      )}
    </li>
  );
}

function EmojiGrid({ list }: { list: PlainEmoji[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {list.map((e) => (
        <li key={e.emoji_id} className="group relative">
          <img src={e.url} alt={`:${e.name}:`} title={`:${e.name}:`} className="h-8 w-8 rounded" loading="lazy" />
        </li>
      ))}
    </ul>
  );
}

export default function EmojiPage() {
  const toast = useToast();
  const { subscribe } = useLive();
  const [data, setData] = useState<EmojiOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<EmojiSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [adding, setAdding] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [deleting, setDeleting] = useState<LibraryEmoji | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    loadCustomEmoji(true)
      .then(setData)
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : "Couldn't load emoji"));
  }, []);
  useEffect(load, [load]);
  useEffect(() => subscribe((e) => e.type === "emoji.changed" && load()), [subscribe, load]);

  // Search as you type (links resolve straight away).
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults(null);
      setSearchError(null);
      return;
    }
    let alive = true;
    const t = window.setTimeout(() => {
      setSearching(true);
      api
        .get<EmojiSearchResult[]>(`/api/emoji/search?q=${encodeURIComponent(q)}`)
        .then((r) => {
          if (!alive) return;
          setResults(r);
          setSearchError(null);
        })
        .catch((e) => alive && setSearchError(e instanceof ApiError ? e.message : "Search failed"))
        .finally(() => alive && setSearching(false));
    }, 450);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [query]);

  const add = async (items: EmojiSearchResult[]) => {
    const keys = items.map((r) => `${r.kind}:${r.game_id}`);
    setAdding((s) => new Set([...s, ...keys]));
    try {
      const { results: done } = await api.post<{ results: EmojiAddResult[] }>("/api/emoji", { items: items.map((r) => ({ kind: r.kind, game_id: r.game_id })) });
      const failed = done.filter((d) => d.status === "error");
      const created = done.filter((d) => d.status === "created").length;
      const shared = done.filter((d) => d.status === "shared").length;
      if (failed.length) toast("error", failed.map((f) => f.error).join(" "));
      if (created || shared)
        toast("ok", [created && `${created} new emoji`, shared && `${shared} reused an existing icon`].filter(Boolean).join(", "));
      setResults((list) =>
        list &&
        list.map((r) => {
          const hit = done.find((d) => d.kind === r.kind && d.game_id === r.game_id && d.emoji);
          return hit ? { ...r, state: "added", emoji: hit.emoji } : r;
        }),
      );
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't add the emoji");
    } finally {
      setAdding((s) => {
        const next = new Set(s);
        keys.forEach((k) => next.delete(k));
        return next;
      });
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.del(`/api/emoji/${deleting.id}`);
      toast("ok", `Deleted :${deleting.name}:`);
      setDeleting(null);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't delete it");
    } finally {
      setBusy(false);
    }
  };

  const library = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (data?.library ?? []).filter((e) => !f || e.name.includes(f) || e.aliases.some((a) => a.name.toLowerCase().includes(f)));
  }, [data, filter]);
  const aliasCount = (data?.library ?? []).reduce((n, e) => n + e.aliases.length, 0);
  const addable = (results ?? []).filter((r) => !r.error && r.state !== "added");
  const canManage = data?.can_manage ?? false;

  return (
    <>
      <SectionHeader
        eyebrow="Communication"
        title="Emoji"
        description="Turn item and spell icons into emoji the bot can use in Discord and on this dashboard. Items and spells that share an icon share one emoji."
      />

      {data && !canManage && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1 text-muted">Adding and deleting emoji is switched off.</span>
          <Link to="/settings?group=features#FEATURE_EMOJI_ADMIN_ENABLED" className="text-xs text-gold hover:underline">
            Turn it on
          </Link>
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {data ? (
          <>
            <StatTile value={`${data.used}/${data.limit}`} label="Bot emoji slots used" icon={<Smile className="h-3 w-3" />} tone={data.used > data.limit * 0.9 ? "warn" : undefined} />
            <StatTile value={data.library.length} label="Icons in the library" icon={<ImagePlus className="h-3 w-3" />} />
            <StatTile value={aliasCount} label="Items and spells covered" icon={<Layers className="h-3 w-3" />} />
            <StatTile value={Math.max(0, aliasCount - data.library.length)} label="Emoji saved by reuse" icon={<Recycle className="h-3 w-3" />} tone={aliasCount > data.library.length ? "ok" : undefined} />
          </>
        ) : (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[76px]" />)
        )}
      </div>

      <section className="card mb-5">
        <div className="border-b border-line p-3 md:px-5 md:py-4">
          <label htmlFor="emoji-search" className="label mb-1.5 block">
            Add an item or spell
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
            <input
              id="emoji-search"
              className="input pl-9 pr-9"
              placeholder="Search a name (e.g. Flash Heal), or paste Wowhead links"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {searching && <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-faint" />}
          </div>
          <p className="mt-1.5 text-2xs text-faint">
            Names and icons come from Blizzard's API (Wowhead only for things Blizzard doesn't have). Links are only used for their ID.
            {data && !data.blizzard_configured && <span className="text-warn"> Blizzard API keys aren't set up, so only pasted links work.</span>}
          </p>
        </div>
        {searchError ? (
          <p className="px-5 py-4 text-crimson">{searchError}</p>
        ) : results === null ? (
          <div className="flex items-center gap-3 px-5 py-5 text-muted">
            <Sparkles className="h-4 w-4 text-faint" /> Results show whether an icon is new, or already used by something else (then it's reused, not uploaded again).
          </div>
        ) : results.length === 0 && !searching ? (
          <p className="px-5 py-4 text-muted">Nothing found for “{query.trim()}”. Try the exact name, or paste a Wowhead link.</p>
        ) : (
          <>
            {addable.length > 1 && canManage && (
              <div className="flex justify-end border-b border-line px-4 py-2 md:px-5">
                <Button size="sm" variant="outline" onClick={() => add(addable.slice(0, 15))} disabled={adding.size > 0}>
                  Add all {Math.min(addable.length, 15)}
                </Button>
              </div>
            )}
            <ul className="divide-y divide-line">
              {results.map((r) => (
                <ResultRow key={`${r.kind}:${r.game_id}`} r={r} onAdd={(x) => add([x])} busy={adding.has(`${r.kind}:${r.game_id}`)} canManage={canManage} />
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="card mb-5">
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3 md:px-5">
          <div className="label flex-1">Library</div>
          <input className="input h-8 w-full py-1 text-xs sm:w-60" placeholder="Filter by emoji, item or spell…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter the library" />
        </header>
        {loadError ? (
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load the emoji">
            {loadError}
          </EmptyState>
        ) : !data ? (
          <div className="grid gap-2 p-4 sm:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-16" />
            ))}
          </div>
        ) : library.length === 0 ? (
          <EmptyState icon={<Smile className="h-6 w-6" />} title={filter ? "Nothing matches" : "No icon emoji yet"}>
            {filter ? "Try another name." : "Search for an item or spell above to add its icon."}
          </EmptyState>
        ) : (
          <ul className="grid gap-2 p-3 sm:grid-cols-2 md:p-4 xl:grid-cols-3">
            {library.map((e) => (
              <LibraryCard key={e.id} e={e} onDelete={setDeleting} canManage={canManage} />
            ))}
          </ul>
        )}
      </section>

      {data && (data.other.length > 0 || data.server.length > 0) && (
        <div className="grid gap-5 lg:grid-cols-2">
          {data.other.length > 0 && (
            <section className="card p-4 md:p-5">
              <div className="label mb-1">Other bot emoji</div>
              <p className="mb-3 text-2xs text-faint">Class, role and spec icons the bot manages itself.</p>
              <EmojiGrid list={data.other} />
            </section>
          )}
          {data.server.length > 0 && (
            <section className="card p-4 md:p-5">
              <div className="label mb-1">Server emoji</div>
              <p className="mb-3 text-2xs text-faint">The server's own custom emoji, also usable in announcements.</p>
              <EmojiGrid list={data.server.filter((e) => e.available !== false)} />
            </section>
          )}
        </div>
      )}

      <Dialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        busy={busy}
        title={`Delete :${deleting?.name ?? ""}:?`}
        actions={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove} loading={busy}>
              Delete emoji
            </Button>
          </>
        }
      >
        <p className="text-muted">
          The emoji is removed from the bot{deleting?.aliases.length ? <> and from {deleting.aliases.map((a) => a.name).join(", ")}</> : null}. Messages that
          already use it will show <span className={cx("font-mono text-fg")}>:{deleting?.name}:</span> as plain text instead. The bot may add it again if a raid summary needs this
          icon.
        </p>
      </Dialog>
    </>
  );
}
