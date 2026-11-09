// Crafting (webapi/crafting_api.py): who in the guild can make what. Pick
// "featured" recipes (e.g. high-value ones) from a profession's list; the
// bot keeps them posted in the crafting channel with who can make them and
// the materials to send.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDown, ArrowUp, EyeOff, Hammer, Plus, RotateCcw, Search, Send, Star, Trash2 } from "lucide-react";
import { ApiError, api, type CraftingOverview, type Crafter, type FeaturedRecipe, type RecipeCatalog } from "../lib/api";
import { useLive } from "../lib/live";
import { WowLink } from "../lib/wowhead";
import { CLASS_COLORS } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Skeleton, StatTile } from "../components/ui";

function CrafterChip({ c }: { c: Crafter }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md border border-line bg-raised/40 px-2 py-0.5 text-xs">
      {c.member && <Avatar url={c.member.avatar_url} name={c.member.name} size="h-4 w-4" />}
      <span className="truncate font-medium" style={{ color: CLASS_COLORS[c.class_name ?? ""] }}>
        {c.display_name}
      </span>
      {c.member && <span className="truncate text-faint">· {c.member.name}</span>}
      {c.skill != null && <span className="shrink-0 tabular-nums text-muted">{c.skill}/{c.max ?? "?"}</span>}
    </span>
  );
}

function RecipeIcon({ url }: { url: string | null }) {
  return url ? (
    <img src={url} alt="" className="h-9 w-9 shrink-0 rounded ring-1 ring-line" loading="lazy" />
  ) : (
    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-raised ring-1 ring-line">
      <Hammer className="h-4 w-4 text-faint" />
    </div>
  );
}

function FeaturedCard({
  r,
  onRemove,
  onMove,
  first,
  last,
  busy,
}: {
  r: FeaturedRecipe;
  onRemove: () => void;
  onMove: (by: -1 | 1) => void;
  first: boolean;
  last: boolean;
  busy: boolean;
}) {
  const name = r.item_name ?? r.name;
  return (
    <li className="flex items-start gap-3 px-4 py-3 md:px-5">
      <div className="flex shrink-0 flex-col">
        <button type="button" onClick={() => onMove(-1)} disabled={busy || first} aria-label={`Move ${name} up`} className="rounded p-0.5 text-faint transition hover:bg-raised hover:text-fg disabled:opacity-30 disabled:hover:bg-transparent">
          <ArrowUp className="h-3.5 w-3.5" />
        </button>
        <button type="button" onClick={() => onMove(1)} disabled={busy || last} aria-label={`Move ${name} down`} className="rounded p-0.5 text-faint transition hover:bg-raised hover:text-fg disabled:opacity-30 disabled:hover:bg-transparent">
          <ArrowDown className="h-3.5 w-3.5" />
        </button>
      </div>
      <RecipeIcon url={r.item_icon_url} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {r.item_url ? (
            <a href={r.item_url} target="_blank" rel="noreferrer" className="font-semibold hover:underline">
              {name}
            </a>
          ) : (
            <span className="font-semibold">{name}</span>
          )}
          {r.quantity && r.quantity > 1 && <span className="text-2xs text-muted">×{r.quantity}</span>}
          {r.category && <span className="text-2xs text-faint">{r.category}</span>}
          {r.hidden_in_channel && (
            <Badge>
              <EyeOff className="h-2.5 w-2.5" /> Hidden in the channel
            </Badge>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {r.crafters.length ? (
            r.crafters.map((c) => <CrafterChip key={c.slug} c={c} />)
          ) : (
            <span className="inline-flex items-center gap-1 text-xs text-warn">
              <AlertTriangle className="h-3.5 w-3.5" /> Nobody in the guild knows this recipe yet
            </span>
          )}
        </div>
        {r.reagents.length > 0 && (
          <p className="mt-1.5 text-2xs text-muted">
            Materials:{" "}
            {r.reagents.map((x, i) => (
              <span key={`${x.id}-${i}`}>
                {i > 0 && " · "}
                {x.quantity ?? 1}× <WowLink kind="item" id={x.id}>{x.name}</WowLink>
              </span>
            ))}
          </p>
        )}
      </div>
      <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={onRemove} disabled={busy} aria-label={`Stop featuring ${name}`}>
        <span className="hidden sm:inline">Remove</span>
      </Button>
    </li>
  );
}

function RecipeBrowser({ data, onAdded }: { data: CraftingOverview; onAdded: () => void }) {
  const toast = useToast();
  const [professionId, setProfessionId] = useState<number | null>(data.professions[0]?.id ?? null);
  const [catalog, setCatalog] = useState<RecipeCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [knownOnly, setKnownOnly] = useState(false);
  const [adding, setAdding] = useState<number | null>(null);

  const load = useCallback(() => {
    if (professionId == null) return;
    setCatalog(null);
    setError(null);
    api
      .get<RecipeCatalog>(`/api/crafting/professions/${professionId}`)
      .then(setCatalog)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the recipes"));
  }, [professionId]);
  useEffect(load, [load]);

  const categories = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (catalog?.categories ?? [])
      .map((c) => ({ ...c, recipes: c.recipes.filter((r) => (!q || r.name.toLowerCase().includes(q)) && (!knownOnly || r.known_by > 0)) }))
      .filter((c) => c.recipes.length > 0);
  }, [catalog, query, knownOnly]);

  const add = async (recipeId: number) => {
    setAdding(recipeId);
    try {
      await api.post("/api/crafting/featured", { recipe_id: recipeId, profession_id: professionId });
      toast("ok", "Featured. The crafting channel updates in a moment.");
      setCatalog((c) => c && { ...c, categories: c.categories.map((cat) => ({ ...cat, recipes: cat.recipes.map((r) => (r.id === recipeId ? { ...r, featured: true } : r)) })) });
      onAdded();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't feature the recipe");
    } finally {
      setAdding(null);
    }
  };

  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Feature a recipe</div>
        <p className="mt-0.5 text-2xs text-faint">Pick a profession, find the recipe and add it. "Known by" counts the guild's crafters.</p>
      </header>
      <div className="flex flex-wrap gap-1.5 border-b border-line px-4 py-3 md:px-5">
        {data.professions.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => setProfessionId(p.id)}
            aria-pressed={p.id === professionId}
            className={`inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs transition ${p.id === professionId ? "border-gold/50 bg-gold/15 text-gold" : "border-line text-muted hover:text-fg"}`}
          >
            {p.icon_url && <img src={p.icon_url} alt="" className="h-4 w-4 rounded-sm" />}
            {p.name}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 md:px-5">
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <input className="input h-9 pl-9" placeholder="Search recipes" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search recipes" />
        </div>
        <label className="inline-flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={knownOnly} onChange={(e) => setKnownOnly(e.target.checked)} /> Only recipes someone knows
        </label>
      </div>
      <div className="max-h-[32rem] overflow-y-auto border-t border-line">
        {error ? (
          <p className="px-4 py-3 text-warn md:px-5">{error}</p>
        ) : !catalog ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
          </div>
        ) : categories.length === 0 ? (
          <p className="px-4 py-3 text-faint md:px-5">No recipes match.</p>
        ) : (
          categories.map((cat) => (
            <div key={cat.name}>
              <div className="sticky top-0 bg-surface/95 px-4 py-1.5 text-2xs font-semibold uppercase tracking-wide text-faint backdrop-blur md:px-5">{cat.name}</div>
              <ul className="divide-y divide-line">
                {cat.recipes.map((r) => (
                  <li key={r.id} className="flex items-center gap-3 px-4 py-1.5 md:px-5">
                    <span className="min-w-0 flex-1 truncate">{r.name}</span>
                    <span className={`shrink-0 text-2xs tabular-nums ${r.known_by ? "text-ok" : "text-faint"}`}>known by {r.known_by}</span>
                    {r.featured ? (
                      <Badge tone="gold">
                        <Star className="h-2.5 w-2.5" /> Featured
                      </Badge>
                    ) : (
                      <Button size="sm" icon={<Plus className="h-3.5 w-3.5" />} loading={adding === r.id} disabled={adding !== null} onClick={() => add(r.id)}>
                        Add
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

export default function CraftingPage() {
  const toast = useToast();
  const { subscribe } = useLive();
  const [data, setData] = useState<CraftingOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<FeaturedRecipe | null>(null);

  const load = useCallback(() => {
    api.get<CraftingOverview>("/api/crafting").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load crafting"));
  }, []);
  useEffect(load, [load]);
  useEffect(() => subscribe((e) => e.type === "crafting.changed" && load()), [subscribe, load]);

  const post = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ result: string | null }>("/api/crafting/post");
      toast("ok", r.result ?? "The crafting channel is up to date.");
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't update the channel");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await api.del(`/api/crafting/featured/${removing.recipe_id}`);
      toast("ok", `${removing.item_name ?? removing.name} isn't featured any more.`);
      setRemoving(null);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't remove it");
    } finally {
      setBusy(false);
    }
  };

  const withCrafters = data?.professions.filter((p) => p.crafters.length) ?? [];

  // The featured recipes per profession, in the order the API gives (the channel's order).
  const groups = useMemo(() => {
    const found: { id: number | null; name: string; rows: FeaturedRecipe[] }[] = [];
    for (const r of data?.featured ?? []) {
      const last = found[found.length - 1];
      if (last && last.id === r.profession_id) last.rows.push(r);
      else found.push({ id: r.profession_id, name: r.profession_name ?? "Other", rows: [r] });
    }
    return found;
  }, [data]);

  const saveOrder = async (professionId: number, recipeIds: number[] | null) => {
    setBusy(true);
    try {
      await api.post("/api/crafting/order", { profession_id: professionId, recipe_ids: recipeIds });
      if (recipeIds === null) toast("ok", "Back to hardest first.");
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't save the order");
    } finally {
      setBusy(false);
    }
  };

  const move = (professionId: number | null, rows: FeaturedRecipe[], index: number, by: -1 | 1) => {
    if (professionId == null) return;
    const ids = rows.map((r) => r.recipe_id);
    [ids[index], ids[index + by]] = [ids[index + by], ids[index]];
    void saveOrder(professionId, ids);
  };

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Crafting"
        description="Who in the guild can make what. Featured recipes are posted in the crafting channel with their crafters and the materials to send."
        actions={
          <Button icon={<Send className="h-3.5 w-3.5" />} onClick={post} loading={busy && !removing} disabled={!data || Boolean(data.channel_problem)} title={data?.channel_problem ?? undefined}>
            Update channel now
          </Button>
        }
      />

      {error ? (
        <div className="card">
          <EmptyState icon={<Hammer className="h-6 w-6" />} title="Crafting isn't available">
            {error}
          </EmptyState>
        </div>
      ) : !data ? (
        <Skeleton className="h-64" />
      ) : (
        <>
          {data.channel_problem && (
            <p className="mb-4 flex items-start gap-2 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3 text-muted">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
              <span>
                {data.channel_problem}{" "}
                <Link to="/settings?group=crafting#CRAFTING_CHANNEL_ID" className="text-gold hover:underline">
                  Open the Crafting settings
                </Link>
              </span>
            </p>
          )}

          <div className="mb-5 grid grid-cols-3 gap-3">
            <StatTile value={data.featured.length} label="Featured recipes" icon={<Star className="h-3 w-3" />} />
            <StatTile value={withCrafters.length} label="Professions in the guild" />
            <StatTile value={`${data.characters_with_recipes}/${data.characters}`} label="Characters with recipes read" />
          </div>

          <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <div className="space-y-5">
              <section className="card">
                <header className="border-b border-line px-4 py-3 md:px-5">
                  <div className="label">Featured recipes</div>
                  <p className="mt-0.5 text-2xs text-faint">
                    {data.hide_unknown_recipes
                      ? "Recipes nobody in the guild knows are left out of the channel."
                      : "Every featured recipe is in the channel, also ones nobody knows yet."}{" "}
                    <Link to="/settings?group=crafting#CRAFTING_HIDE_UNKNOWN_RECIPES" className="text-gold hover:underline">
                      Change
                    </Link>
                  </p>
                </header>
                {data.featured.length ? (
                  groups.map((g) => {
                    const custom = data.custom_order.includes(g.id ?? -1);
                    const icon = data.professions.find((p) => p.id === g.id)?.icon_url;
                    return (
                      <div key={g.id ?? "other"} className="border-b border-line last:border-b-0">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 bg-raised/30 px-4 py-2 md:px-5">
                          {icon && <img src={icon} alt="" className="h-4 w-4 rounded-sm" />}
                          <span className="font-semibold">{g.name}</span>
                          <span className="text-2xs text-faint">{custom ? "Your order" : "Hardest first"}</span>
                          {custom && g.id != null && (
                            <button type="button" onClick={() => saveOrder(g.id!, null)} disabled={busy} className="ml-auto inline-flex items-center gap-1 text-2xs text-gold hover:underline disabled:opacity-50">
                              <RotateCcw className="h-3 w-3" /> Back to hardest first
                            </button>
                          )}
                        </div>
                        <ul className="divide-y divide-line">
                          {g.rows.map((r, i) => (
                            <FeaturedCard
                              key={r.recipe_id}
                              r={r}
                              busy={busy}
                              first={i === 0}
                              last={i === g.rows.length - 1}
                              onRemove={() => setRemoving(r)}
                              onMove={(by) => move(g.id, g.rows, i, by)}
                            />
                          ))}
                        </ul>
                      </div>
                    );
                  })
                ) : (
                  <EmptyState icon={<Star className="h-6 w-6" />} title="No featured recipes yet">
                    Add the recipes people should know about, e.g. high-value ones, below.
                  </EmptyState>
                )}
              </section>
              <RecipeBrowser data={data} onAdded={load} />
            </div>

            <section className="card self-start">
              <header className="border-b border-line px-4 py-3 md:px-5">
                <div className="label">Professions</div>
                <p className="mt-0.5 text-2xs text-faint">Read from Blizzard when a player logs out.</p>
              </header>
              <ul className="divide-y divide-line">
                {data.professions.map((p) => (
                  <li key={p.id} className="px-4 py-3 md:px-5">
                    <div className="mb-1.5 flex items-center gap-2">
                      {p.icon_url && <img src={p.icon_url} alt="" className="h-5 w-5 rounded-sm ring-1 ring-line" />}
                      <span className="font-semibold">{p.name}</span>
                      <span className="text-2xs text-faint">
                        {p.crafters.length} crafter{p.crafters.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    {p.crafters.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {p.crafters.map((c) => (
                          <CrafterChip key={c.slug} c={c} />
                        ))}
                      </div>
                    ) : (
                      <p className="text-2xs text-faint">Nobody in the guild yet.</p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </>
      )}

      <Dialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Stop featuring this recipe?"
        busy={busy}
        actions={
          <>
            <Button variant="ghost" onClick={() => setRemoving(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" loading={busy} onClick={remove} icon={<Trash2 className="h-3.5 w-3.5" />}>
              Remove
            </Button>
          </>
        }
      >
        <p className="text-muted">{removing?.item_name ?? removing?.name} is taken out of the crafting channel. You can add it again at any time.</p>
      </Dialog>
    </>
  );
}
