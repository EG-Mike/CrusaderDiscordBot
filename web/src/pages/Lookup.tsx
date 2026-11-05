// Character lookup: a gear check started by moderators, for any character
// in the region (webapi/lookup_api.py). The name is in the address
// (/lookup?name=...), so a lookup can be linked or bookmarked.
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, BookOpen, Gem, Search, Shield, Sparkles, UserPlus, Users } from "lucide-react";
import { ApiError, api, type GearItem, type LookupResult, type RosterCharacter } from "../lib/api";
import { timeAgo } from "../lib/time";
import { CLASS_COLORS, QUALITY_COLORS } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Skeleton, StatTile } from "../components/ui";

const RECENT_KEY = "lookup.recent";
const COSMETIC = new Set(["SHIRT", "TABARD"]);

function readRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]") as string[];
  } catch {
    return [];
  }
}

function remember(name: string) {
  try {
    const list = [name, ...readRecent().filter((n) => n.toLowerCase() !== name.toLowerCase())].slice(0, 8);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch {
    /* not remembered in a private window */
  }
}

function ItemRow({ item }: { item: GearItem }) {
  const extras = [...item.enchants, ...item.sockets.map((s) => s.gem ?? `Empty ${s.type ?? "socket"}`)];
  return (
    <li className="flex items-start gap-3 px-4 py-2 md:px-5">
      {item.icon_url ? (
        <img src={item.icon_url} alt="" className="mt-0.5 h-8 w-8 shrink-0 rounded ring-1 ring-line" loading="lazy" />
      ) : (
        <div className="mt-0.5 h-8 w-8 shrink-0 rounded bg-raised ring-1 ring-line" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium" style={{ color: QUALITY_COLORS[item.quality ?? ""] }}>
            {item.name}
          </span>
          <span className="text-2xs text-faint">{item.slot_name}</span>
          {item.set && <span className="text-2xs text-gold">{item.set}</span>}
        </div>
        {extras.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-2xs">
            {item.enchants.map((e) => (
              <span key={e} className="inline-flex items-center gap-1 text-ok">
                <Sparkles className="h-3 w-3" /> {e}
              </span>
            ))}
            {item.sockets.map((s, i) => (
              <span key={i} className={s.gem ? "inline-flex items-center gap-1 text-info" : "inline-flex items-center gap-1 text-warn"}>
                <Gem className="h-3 w-3" /> {s.gem ?? `Empty ${s.type ?? "socket"}`}
              </span>
            ))}
          </div>
        )}
        {item.stats.length > 0 && <div className="mt-0.5 truncate text-2xs text-faint">{item.stats.join(" · ")}</div>}
      </div>
      <span className="shrink-0 font-semibold tabular-nums">{item.item_level ?? "?"}</span>
    </li>
  );
}

export default function LookupPage() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const name = params.get("name") ?? "";
  const [draft, setDraft] = useState(name);
  const [result, setResult] = useState<LookupResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [adding, setAdding] = useState(false);
  const [recent, setRecent] = useState(readRecent);

  useEffect(() => {
    setDraft(name);
    if (!name) {
      setResult(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<LookupResult>(`/api/lookup?name=${encodeURIComponent(name)}`)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        remember(r.character.character);
        setRecent(readRecent());
      })
      .catch((e) => !cancelled && (setResult(null), setError(e instanceof ApiError ? e.message : "The lookup didn't work")))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [name]);

  const search = (e: FormEvent) => {
    e.preventDefault();
    if (draft.trim()) setParams({ name: draft.trim() });
  };

  const addToRoster = async () => {
    if (!result) return;
    setAdding(true);
    try {
      const c = await api.post<RosterCharacter>("/api/roster/characters", { name: result.character.slug });
      toast("ok", `${c.display_name} is on the roster. Link it to a member there.`);
      setResult({ ...result, roster: { on_roster: true, in_guild: Boolean(c.guild_slug), is_main: false, member: null } });
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Couldn't add the character");
    } finally {
      setAdding(false);
    }
  };

  const c = result?.character;
  const flags = c
    ? [
        ...(result!.off_ruleset ? [`On the ${c.ruleset ?? "another"} ruleset, not one of the guild's`] : []),
        ...(c.empty_slots.length ? [`Empty: ${c.empty_slots.join(", ")}`] : []),
        ...(c.missing_enchants.length ? [`No enchant: ${c.missing_enchants.join(", ")}`] : []),
        ...(c.empty_sockets ? [`${c.empty_sockets} empty gem socket${c.empty_sockets === 1 ? "" : "s"}`] : []),
      ]
    : [];

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Character lookup"
        description="Look up any character in the region on Blizzard: gear, enchants, talents and professions as of their last logout."
      />

      <form onSubmit={search} className="mb-3 flex max-w-xl gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <input className="input pl-9" placeholder="Name and surname, e.g. Eggmike Thewhite" value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
        </div>
        <Button type="submit" variant="primary" loading={loading} disabled={!draft.trim()}>
          Look up
        </Button>
      </form>
      {recent.length > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-1.5 text-2xs">
          <span className="text-faint">Recent:</span>
          {recent.map((n) => (
            <button key={n} type="button" onClick={() => setParams({ name: n })} className="rounded-md border border-line px-2 py-0.5 text-muted transition hover:text-fg">
              {n}
            </button>
          ))}
        </div>
      )}

      {loading && !result ? (
        <Skeleton className="h-64" />
      ) : error ? (
        <div className="card">
          <EmptyState icon={<Search className="h-6 w-6" />} title="Not found">
            {error}
          </EmptyState>
        </div>
      ) : !c ? (
        <div className="card">
          <EmptyState icon={<Search className="h-6 w-6" />} title="Look someone up">
            Characters can be found from level 10, after logging out once. Their data is from their last logout.
          </EmptyState>
        </div>
      ) : (
        <div className={loading ? "opacity-60 transition" : "transition"}>
          <section className="card mb-5 flex flex-wrap items-center gap-x-6 gap-y-3 px-4 py-4 md:px-5">
            <div className="min-w-0 flex-1">
              <h2 className="text-lg font-semibold" style={{ color: CLASS_COLORS[c.class_name] }}>
                {c.character}
              </h2>
              <p className="text-muted">
                Level {c.level ?? "?"} {[c.race, c.class_name].filter(Boolean).join(" ")}
                {c.guild ? ` · <${c.guild}>` : " · no guild"}
                {c.ruleset ? ` · ${c.ruleset} ruleset` : ""}
                {c.last_login ? ` · last login ${timeAgo(new Date(c.last_login).toISOString())}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {result!.roster?.member ? (
                <Link to="/roster" className="inline-flex items-center gap-2 rounded-lg border border-line px-2.5 py-1 text-xs transition hover:bg-raised">
                  <Avatar url={result!.roster.member.avatar_url} name={result!.roster.member.name} size="h-5 w-5" />
                  {result!.roster.member.name}
                  {result!.roster.is_main && <Badge tone="gold">Main</Badge>}
                </Link>
              ) : result!.roster ? (
                <Link to="/roster" className="inline-flex items-center gap-1.5 text-xs text-gold hover:underline">
                  <Users className="h-3.5 w-3.5" /> On the roster, not linked
                </Link>
              ) : (
                <Button size="sm" icon={<UserPlus className="h-3.5 w-3.5" />} loading={adding} onClick={addToRoster}>
                  Add to roster
                </Button>
              )}
            </div>
          </section>

          <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile value={c.equipped_item_level ?? "?"} label="Item level (equipped)" icon={<Shield className="h-3 w-3" />} />
            <StatTile value={c.level ?? "?"} label="Level" />
            <StatTile value={c.talents.map((t) => t.points).join(" / ") || "0"} label={c.talents.map((t) => t.spec).join(" / ") || "Talents"} />
            <StatTile value={c.professions.filter((p) => p.primary).length} label="Professions" icon={<BookOpen className="h-3 w-3" />} />
          </div>

          {(flags.length > 0 || result!.requirement_problems.length > 0) && (
            <div className="mb-5 space-y-1 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
              {result!.requirement_problems.length > 0 && (
                <p className="flex items-start gap-2 text-warn">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> Below the guild's minimum: {result!.requirement_problems.join(", ")}
                </p>
              )}
              {flags.map((f) => (
                <p key={f} className="flex items-start gap-2 text-muted">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" /> {f}
                </p>
              ))}
            </div>
          )}

          <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <section className="card">
              <header className="border-b border-line px-4 py-3 md:px-5">
                <div className="label">Gear</div>
              </header>
              <ul className="divide-y divide-line">
                {c.items.filter((i) => !COSMETIC.has(i.slot)).map((item) => (
                  <ItemRow key={item.slot} item={item} />
                ))}
                {c.items.length === 0 && <li className="px-4 py-3 text-faint md:px-5">No gear equipped.</li>}
              </ul>
            </section>

            <div className="space-y-5">
              <section className="card">
                <header className="border-b border-line px-4 py-3 md:px-5">
                  <div className="label">Talents</div>
                </header>
                <ul className="divide-y divide-line">
                  {c.talents.map((t) => (
                    <li key={t.spec} className="flex items-center justify-between px-4 py-2 md:px-5">
                      {t.spec}
                      <span className="font-semibold tabular-nums">{t.points}</span>
                    </li>
                  ))}
                  {c.talents.length === 0 && <li className="px-4 py-3 text-faint md:px-5">No talents spent.</li>}
                </ul>
              </section>
              <section className="card">
                <header className="border-b border-line px-4 py-3 md:px-5">
                  <div className="label">Professions</div>
                </header>
                <ul className="divide-y divide-line">
                  {c.professions.map((p) => (
                    <li key={p.name} className="flex items-center justify-between px-4 py-2 md:px-5">
                      <span>
                        {p.name} {!p.primary && <span className="text-2xs text-faint">secondary</span>}
                      </span>
                      <span className="tabular-nums text-muted">
                        {p.skill ?? "?"} / {p.max ?? "?"}
                      </span>
                    </li>
                  ))}
                  {c.professions.length === 0 && <li className="px-4 py-3 text-faint md:px-5">None.</li>}
                </ul>
              </section>
              <section className="card px-4 py-3 md:px-5">
                <div className="label mb-1">WarcraftLogs</div>
                <p className="text-faint">Parses show here once WarcraftLogs covers WoW Forever.</p>
              </section>
            </div>
          </div>
          {c.profile_updated && <p className="mt-4 text-2xs text-faint">Blizzard last updated this character on {new Date(c.profile_updated).toLocaleString()}.</p>}
        </div>
      )}
    </>
  );
}
