// Card statistics (webapi/applications_api.py, card_stats.py): which
// achievement statistics (e.g. raid boss kills) the application card,
// /lookup and the application pages show, in order. Blizzard has no list of
// every statistic, so the list to pick from is what the bot has seen on
// characters; "Scan the guild" reads the guild's members to find more.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowLeft, ArrowUp, BarChart3, Plus, Radar, Search, X } from "lucide-react";
import { ApiError, api, type CardStatistic, type CardStatisticsConfig } from "../lib/api";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Skeleton } from "../components/ui";

const BOSS_KILLS = "Dungeons & Raids > Boss Kills";

/** Statistics grouped as on the card: consecutive ones of the same instance share a line. */
function groupRows(stats: CardStatistic[]) {
  const groups: { group: string | null; items: CardStatistic[] }[] = [];
  for (const s of stats) {
    const last = groups[groups.length - 1];
    if (last && last.group === s.group) last.items.push(s);
    else groups.push({ group: s.group, items: [s] });
  }
  return groups;
}

export default function CardStatisticsPage() {
  const toast = useToast();
  const [data, setData] = useState<CardStatisticsConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>(BOSS_KILLS);

  const load = useCallback(() => {
    api.get<CardStatisticsConfig>("/api/applications/statistics").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the statistics"));
  }, []);
  useEffect(load, [load]);

  const save = async (ids: number[]) => {
    setBusy(true);
    try {
      const r = await api.put<{ chosen: CardStatistic[] }>("/api/applications/statistics", { stat_ids: ids });
      setData((d) => d && { ...d, chosen: r.chosen, catalog: d.catalog.map((s) => ({ ...s, chosen: ids.includes(s.id) })) });
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't save");
    } finally {
      setBusy(false);
    }
  };

  const scan = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ read: number; failed: number; statistics: number }>("/api/applications/statistics/scan");
      toast("ok", `Read ${r.read} characters${r.failed ? ` (${r.failed} failed)` : ""}: ${r.statistics} statistics known.`);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "The scan didn't work");
    } finally {
      setBusy(false);
    }
  };

  const chosenIds = data?.chosen.map((s) => s.id) ?? [];
  const add = (ids: number[]) => save([...chosenIds, ...ids.filter((id) => !chosenIds.includes(id))]);
  const remove = (id: number) => save(chosenIds.filter((x) => x !== id));
  const move = (index: number, by: -1 | 1) => {
    const ids = [...chosenIds];
    [ids[index], ids[index + by]] = [ids[index + by], ids[index]];
    void save(ids);
  };

  const categories = useMemo(() => [...new Set((data?.catalog ?? []).map((s) => s.category ?? "Other"))], [data]);
  const browse = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = (data?.catalog ?? []).filter((s) => (q ? s.name.toLowerCase().includes(q) : (s.category ?? "Other") === category));
    const byGroup = new Map<string, CardStatistic[]>();
    for (const s of rows) {
      const key = s.group ?? s.category ?? "Other";
      byGroup.set(key, [...(byGroup.get(key) ?? []), s]);
    }
    return [...byGroup.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [data, query, category]);

  return (
    <>
      <Link to="/applications" className="mb-3 inline-flex items-center gap-1 text-xs text-muted hover:text-fg">
        <ArrowLeft className="h-3.5 w-3.5" /> Applications
      </Link>
      <SectionHeader
        eyebrow="Applications"
        title="Card statistics"
        description="Which of a character's statistics the application card shows, e.g. raid boss kills. Bosses of the same instance share one line; one a character hasn't killed shows 0."
        actions={
          <Button icon={<Radar className="h-3.5 w-3.5" />} onClick={scan} loading={busy} disabled={!data?.can_scan} title={data?.can_scan ? undefined : "Needs the Blizzard API keys and the guild roster"}>
            Scan the guild
          </Button>
        }
      />

      {error ? (
        <div className="card">
          <EmptyState icon={<BarChart3 className="h-6 w-6" />} title="Couldn't load">
            {error}
          </EmptyState>
        </div>
      ) : !data ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <section className="card self-start">
            <header className="border-b border-line px-4 py-3 md:px-5">
              <div className="label">On the card</div>
              <p className="mt-0.5 text-2xs text-faint">
                Heading: <span className="text-muted">{data.title || "none"}</span>{" "}
                <Link to="/settings?group=applications#APPLICATION_STATISTICS_TITLE" className="text-gold hover:underline">
                  Change
                </Link>
              </p>
            </header>
            {data.chosen.length === 0 ? (
              <EmptyState icon={<BarChart3 className="h-6 w-6" />} title="Nothing chosen yet">
                Add statistics from the list, e.g. a raid's bosses with "Add all".
              </EmptyState>
            ) : (
              <>
                <ul className="divide-y divide-line">
                  {data.chosen.map((s, i) => (
                    <li key={s.id} className="flex items-center gap-2 px-4 py-1.5 md:px-5">
                      <div className="flex shrink-0">
                        <button type="button" onClick={() => move(i, -1)} disabled={busy || i === 0} aria-label={`Move ${s.name} up`} className="rounded p-0.5 text-faint hover:text-fg disabled:opacity-30">
                          <ArrowUp className="h-3.5 w-3.5" />
                        </button>
                        <button type="button" onClick={() => move(i, 1)} disabled={busy || i === data.chosen.length - 1} aria-label={`Move ${s.name} down`} className="rounded p-0.5 text-faint hover:text-fg disabled:opacity-30">
                          <ArrowDown className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      <span className="min-w-0 flex-1 truncate">
                        {s.label}
                        {s.group && <span className="ml-1.5 text-2xs text-faint">{s.group}</span>}
                      </span>
                      <button type="button" onClick={() => remove(s.id)} disabled={busy} aria-label={`Remove ${s.name}`} className="rounded p-1 text-faint hover:text-crimson disabled:opacity-30">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="border-t border-line px-4 py-3 md:px-5">
                  <div className="label mb-1.5">On the card it looks like</div>
                  <div className="space-y-0.5 rounded-lg bg-raised/40 px-3 py-2 text-xs">
                    {data.title && <div className="font-semibold">{data.title}</div>}
                    {groupRows(data.chosen).map((g, i) => (
                      <div key={i}>
                        {g.group && <span className="font-semibold">{g.group} </span>}
                        <span className="text-muted">{g.items.map((s) => `${s.label} 3`).join(" · ")}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </>
            )}
          </section>

          <section className="card">
            <header className="border-b border-line px-4 py-3 md:px-5">
              <div className="label">Statistics seen</div>
              <p className="mt-0.5 text-2xs text-faint">
                {data.catalog.length} from characters the bot has read. A boss shows up here once someone has killed it; scan the guild to find more.
              </p>
            </header>
            <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3 md:px-5">
              <select className="input h-9 w-auto max-w-full py-1 text-xs" value={category} onChange={(e) => setCategory(e.target.value)} disabled={Boolean(query)} aria-label="Category">
                {!categories.includes(BOSS_KILLS) && <option value={BOSS_KILLS}>{BOSS_KILLS}</option>}
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <div className="relative min-w-[10rem] flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
                <input className="input h-9 pl-9" placeholder="Search all statistics" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search statistics" />
              </div>
            </div>
            {browse.length === 0 ? (
              <p className="px-4 py-3 text-faint md:px-5">{data.catalog.length ? "Nothing matches." : "No statistics seen yet: scan the guild, or wait for the next application."}</p>
            ) : (
              <div className="max-h-[36rem] overflow-y-auto">
                {browse.map(([group, stats]) => {
                  const missing = stats.filter((s) => !s.chosen).map((s) => s.id);
                  return (
                    <div key={group} className="border-b border-line last:border-b-0">
                      <div className="flex items-center gap-2 bg-raised/30 px-4 py-1.5 md:px-5">
                        <span className="font-semibold">{group}</span>
                        <span className="text-2xs text-faint">{stats.length}</span>
                        {missing.length > 1 && (
                          <Button size="sm" variant="ghost" className="ml-auto" icon={<Plus className="h-3.5 w-3.5" />} disabled={busy} onClick={() => add(missing)}>
                            Add all
                          </Button>
                        )}
                      </div>
                      <ul className="divide-y divide-line">
                        {stats.map((s) => (
                          <li key={s.id} className="flex items-center gap-3 px-4 py-1.5 md:px-5">
                            <span className="min-w-0 flex-1 truncate">{s.label}</span>
                            {s.chosen ? (
                              <Badge tone="gold">On the card</Badge>
                            ) : (
                              <Button size="sm" icon={<Plus className="h-3.5 w-3.5" />} disabled={busy} onClick={() => add([s.id])}>
                                Add
                              </Button>
                            )}
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}
