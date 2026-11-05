// Guild roster: every character the bot knows from Blizzard (the guild's
// roster plus characters members linked), who they belong to, and their
// specs. Moderators link characters to members here, as in Discord.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Link2, RefreshCw, Search, Star, UserPlus, Users } from "lucide-react";
import { ApiError, api, type RosterCharacter, type RosterMember, type RosterOverview, type WowClass } from "../lib/api";
import { useLive } from "../lib/live";
import { timeAgo } from "../lib/time";
import { classColor, className, specName } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { useToast } from "../components/Toast";
import { CharacterName } from "../components/CharacterName";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Segmented, Skeleton, StatTile } from "../components/ui";

type View = "all" | "linked" | "unlinked" | "left";

const VIEWS: [View, string][] = [
  ["all", "All"],
  ["linked", "Linked"],
  ["unlinked", "Not linked"],
  ["left", "Not in the guild"],
];

function MemberPicker({ onPick, busy }: { onPick: (m: RosterMember) => void; busy: boolean }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<RosterMember[]>([]);
  useEffect(() => {
    const t = window.setTimeout(() => {
      api
        .get<{ members: RosterMember[] }>(`/api/roster/members?q=${encodeURIComponent(q)}`)
        .then((d) => setResults(d.members))
        .catch(() => setResults([]));
    }, 200);
    return () => window.clearTimeout(t);
  }, [q]);
  return (
    <div>
      <input className="input" placeholder="Search a member by name" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
      <ul className="mt-2 max-h-56 overflow-y-auto rounded-lg border border-line">
        {results.map((m) => (
          <li key={m.id}>
            <button
              type="button"
              disabled={busy}
              onClick={() => onPick(m)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-raised disabled:opacity-50"
            >
              <Avatar url={m.avatar_url} name={m.name} />
              {m.name}
            </button>
          </li>
        ))}
        {results.length === 0 && <li className="px-3 py-2 text-faint">No members match.</li>}
      </ul>
    </div>
  );
}

function CharacterDialog({
  c,
  classes,
  onClose,
  onChanged,
}: {
  c: RosterCharacter;
  classes: WowClass[];
  onClose: () => void;
  onChanged: (c: RosterCharacter) => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [linking, setLinking] = useState(!c.discord_id);
  const specs = classes.find((k) => k.id === c.class_id)?.specs ?? [];

  const act = async (body: Record<string, unknown>, done: string) => {
    setBusy(true);
    try {
      const updated = await api.post<RosterCharacter>(`/api/roster/characters/${c.slug}`, body);
      onChanged(updated);
      toast("ok", done);
      setLinking(false);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That didn't work");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onClose={onClose} title={c.display_name} busy={busy}>
      <dl className="mb-4 grid grid-cols-[100px_minmax(0,1fr)] gap-y-1 text-[13px]">
        <dt className="text-faint">Level</dt>
        <dd>{c.level ?? "?"}</dd>
        <dt className="text-faint">Class</dt>
        <dd style={{ color: classColor(classes, c.class_id) }}>{className(classes, c.class_id) || "?"}</dd>
        <dt className="text-faint">Guild rank</dt>
        <dd>{c.guild_rank === null ? (c.guild_slug ? "?" : "Not in the guild") : c.guild_rank === 0 ? "Guild master" : `Rank ${c.guild_rank}`}</dd>
        <dt className="text-faint">Last login</dt>
        <dd>{c.last_login ? timeAgo(new Date(c.last_login).toISOString()) : "Unknown"}</dd>
      </dl>

      <div className="label mb-1.5">Member</div>
      {c.member && !linking ? (
        <div className="flex flex-wrap items-center gap-2">
          <Avatar url={c.member.avatar_url} name={c.member.name} />
          <span className="font-medium">{c.member.name}</span>
          {c.is_main ? <Badge tone="gold">Main</Badge> : <Badge>Alt</Badge>}
          <span className="flex-1" />
          {!c.is_main && (
            <Button size="sm" icon={<Star className="h-3.5 w-3.5" />} disabled={busy} onClick={() => act({ action: "main" }, `${c.display_name} is now the main.`)}>
              Make main
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setLinking(true)}>
            Change
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => act({ action: "unlink" }, `Unlinked ${c.display_name}.`)}>
            Unlink
          </Button>
        </div>
      ) : c.discord_id && !c.member && !linking ? (
        <p className="text-muted">
          Linked to a member who isn't in the server any more.{" "}
          <button className="text-gold hover:underline" onClick={() => setLinking(true)}>
            Link to someone else
          </button>
        </p>
      ) : (
        <MemberPicker busy={busy} onPick={(m) => act({ action: "link", discord_id: m.id }, `Linked ${c.display_name} to ${m.name}.`)} />
      )}

      {c.discord_id && specs.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {(
            [
              ["main_spec_id", "Main spec", c.main_spec_id],
              ["off_spec_id", "Off spec", c.off_spec_id],
            ] as const
          ).map(([key, label, value]) => (
            <label key={key} className="block">
              <span className="label mb-1 block">{label}</span>
              <select
                className="input"
                value={value ?? ""}
                disabled={busy}
                onChange={(e) =>
                  act(
                    { action: "specs", main_spec_id: c.main_spec_id, off_spec_id: c.off_spec_id, [key]: e.target.value || null },
                    "Specs saved.",
                  )
                }
              >
                <option value="">Not set</option>
                {specs.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}
    </Dialog>
  );
}

export default function RosterPage() {
  const toast = useToast();
  const { subscribe } = useLive();
  const [data, setData] = useState<RosterOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [view, setView] = useState<View>("all");
  const [open, setOpen] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");

  const load = useCallback(() => {
    api
      .get<RosterOverview>("/api/roster")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the roster"));
  }, []);
  useEffect(() => {
    load();
    return subscribe((event) => event.type === "roster.changed" && load());
  }, [load, subscribe]);

  const replace = (c: RosterCharacter) => setData((d) => d && { ...d, characters: d.characters.map((x) => (x.slug === c.slug ? c : x)) });

  const sync = async () => {
    setSyncing(true);
    try {
      const r = await api.post<{ members: number | null; level_ups: number; announced: number; last_sync: { ok: boolean; message: string | null } }>("/api/roster/sync");
      if (r.last_sync?.ok === false) toast("error", r.last_sync.message ?? "The sync didn't work");
      else toast("ok", `Synced${r.members !== null ? `: ${r.members} guild members` : ""}${r.level_ups ? `, ${r.level_ups} level-ups` : ""}.`);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "The sync didn't work");
    } finally {
      setSyncing(false);
    }
  };

  const add = async () => {
    setAdding(true);
    try {
      const c = await api.post<RosterCharacter>("/api/roster/characters", { name: newName });
      toast("ok", `${c.display_name} is on the roster.`);
      setNewName("");
      load();
      setOpen(c.slug);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't add that character");
    } finally {
      setAdding(false);
    }
  };

  const chars = data?.characters ?? [];
  const classes = data?.classes ?? [];
  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return chars.filter((c) => {
      if (view === "linked" && !c.discord_id) return false;
      if (view === "unlinked" && c.discord_id) return false;
      if (view === "left" && c.guild_slug) return false;
      if (!needle) return true;
      return c.display_name.toLowerCase().includes(needle) || (c.member?.name.toLowerCase().includes(needle) ?? false);
    });
  }, [chars, q, view]);
  const selected = chars.find((c) => c.slug === open) ?? null;

  if (error) return <EmptyState title="Couldn't load the roster">{error}</EmptyState>;

  const linked = chars.filter((c) => c.discord_id).length;
  const members = new Set(chars.filter((c) => c.discord_id).map((c) => c.discord_id)).size;

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Guild roster"
        description="Every character the bot knows from Blizzard, and who they belong to. Members link their own characters with /character link."
        actions={
          <Button icon={<RefreshCw className="h-3.5 w-3.5" />} loading={syncing} onClick={sync}>
            Sync now
          </Button>
        }
      />

      {data && !data.guild_name && (
        <div className="mb-5 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3 text-muted">
          <span className="font-medium text-warn">No guild set.</span> Only characters members link themselves are tracked.{" "}
          <Link to="/settings?group=roster#GUILD_ROSTER_GUILD" className="text-gold hover:underline">
            Set the guild name
          </Link>
        </div>
      )}
      {data?.last_sync && !data.last_sync.ok && (
        <div className="mb-5 rounded-xl border border-crimson/30 bg-crimson/[0.06] px-4 py-3 text-muted">
          <span className="font-medium text-crimson">The last sync didn't work:</span> {data.last_sync.message}
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {data ? (
          <>
            <StatTile value={chars.length} label={data.guild_name ? `Characters · ${data.guild_name}` : "Characters"} icon={<Users className="h-3 w-3" />} />
            <StatTile value={linked} label="Linked to a member" icon={<Link2 className="h-3 w-3" />} />
            <StatTile value={members} label="Members with characters" icon={<UserPlus className="h-3 w-3" />} />
            <StatTile value={data.last_sync ? timeAgo(data.last_sync.at) : "never"} label="Last synced" icon={<RefreshCw className="h-3 w-3" />} />
          </>
        ) : (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[76px]" />)
        )}
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <input className="input pl-9" placeholder="Search characters or members" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <Segmented value={view} options={VIEWS} onChange={setView} label="Show" />
      </div>

      <section className="card overflow-hidden">
        {!data ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState icon={<Users className="h-6 w-6" />} title={chars.length ? "Nothing matches" : "Nobody on the roster yet"}>
            {chars.length ? "Try another search or view." : "Set the guild name in Settings, then Sync now."}
          </EmptyState>
        ) : (
          <table className="w-full text-[13px]">
            <thead className="hidden border-b border-line text-left text-2xs uppercase tracking-wider text-faint md:table-header-group">
              <tr>
                <th className="px-4 py-2 font-semibold md:px-5">Character</th>
                <th className="px-3 py-2 text-right font-semibold">Level</th>
                <th className="px-3 py-2 font-semibold">Class and specs</th>
                <th className="px-3 py-2 font-semibold">Member</th>
                <th className="px-4 py-2 text-right font-semibold md:px-5">Last login</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {visible.map((c) => {
                const specs = [specName(classes, c.main_spec_id), specName(classes, c.off_spec_id)].filter(Boolean).join(" / ");
                return (
                  <tr key={c.slug} onClick={() => setOpen(c.slug)} className="cursor-pointer transition hover:bg-raised/60">
                    <td className="px-4 py-2.5 md:px-5">
                      <div className="flex items-center gap-1.5">
                        {c.is_main && <Star className="h-3 w-3 shrink-0 fill-gold text-gold" aria-label="Main" />}
                        <CharacterName c={c} classes={classes} />
                        {!c.guild_slug && <Badge className="ml-1">Not in guild</Badge>}
                      </div>
                      {/* phone: the other columns as one line */}
                      <div className="mt-0.5 text-2xs text-faint md:hidden">
                        Level {c.level ?? "?"} · {className(classes, c.class_id)}
                        {specs && ` · ${specs}`}
                        {c.member && ` · ${c.member.name}`}
                      </div>
                    </td>
                    <td className="hidden px-3 py-2.5 text-right tabular-nums md:table-cell">{c.level ?? "?"}</td>
                    <td className="hidden px-3 py-2.5 text-muted md:table-cell">
                      {className(classes, c.class_id)}
                      {specs && <span className="text-faint"> · {specs}</span>}
                    </td>
                    <td className="hidden px-3 py-2.5 md:table-cell">
                      {c.member ? (
                        <span className="inline-flex items-center gap-2">
                          <Avatar url={c.member.avatar_url} name={c.member.name} size="h-5 w-5" />
                          {c.member.name}
                        </span>
                      ) : (
                        <span className="text-faint">{c.discord_id ? "Left the server" : "Not linked"}</span>
                      )}
                    </td>
                    <td className="hidden px-4 py-2.5 text-right text-faint md:table-cell md:px-5">
                      {c.last_login ? timeAgo(new Date(c.last_login).toISOString()) : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="card mt-5 p-4 md:p-5">
        <h3 className="font-semibold">Add a character that isn't in the guild</h3>
        <p className="mt-0.5 text-muted">Found on Blizzard by name and surname. Characters can be found from level 10, after logging out once.</p>
        <div className="mt-3 flex max-w-md gap-2">
          <input className="input" placeholder="e.g. Eggmike Thewhite" value={newName} onChange={(e) => setNewName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && newName.trim() && add()} />
          <Button loading={adding} disabled={!newName.trim()} onClick={add}>
            Add
          </Button>
        </div>
      </section>

      {selected && <CharacterDialog c={selected} classes={classes} onClose={() => setOpen(null)} onChanged={replace} />}
    </>
  );
}
