// Rank sync (webapi/rank_sync_api.py, rank_sync.py): members whose Discord
// role (e.g. Fresh) comes with a higher in-game rank than they have, so
// moderators know whom to promote in the game. Rules (role -> rank) and the
// ranks' names (Blizzard only gives numbers) are set here.
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ChevronsUp, Plus, Send, X } from "lucide-react";
import { ApiError, api, type RankSyncEntry, type RankSyncOverview, type RankSyncStatus } from "../lib/api";
import { useGuildLookups } from "../lib/guild";
import { useLive } from "../lib/live";
import { timeAgo } from "../lib/time";
import { classColor } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Skeleton, cx } from "../components/ui";

const GROUPS: { status: RankSyncStatus; label: string; hint: string }[] = [
  { status: "promote", label: "To promote", hint: "In the guild, but below the rank their role comes with. Promote them in the game." },
  { status: "unknown_rank", label: "Rank unknown", hint: "In the guild, but Blizzard hasn't reported their rank yet. It shows after the next roster update." },
  { status: "not_in_guild", label: "Not in the guild", hint: "Their claimed character isn't in the guild (yet). Invite them in the game." },
  { status: "no_character", label: "No character", hint: "They have the role but haven't claimed a character (Claim a character in the roster channel)." },
  { status: "ok", label: "Up to date", hint: "At the rank their role comes with, or higher." },
];

function rankText(rank: number, names: Map<number, string>) {
  const name = names.get(rank);
  return name ? `${name} (${rank})` : rank === 0 ? "Guild master (0)" : `Rank ${rank}`;
}

function EntryRow({ e, overview }: { e: RankSyncEntry; overview: RankSyncOverview }) {
  const c = e.character;
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 md:px-5">
      <span className="inline-flex min-w-0 items-center gap-2">
        <Avatar url={e.member.avatar_url} name={e.member.name} />
        <span className="truncate">{e.member.name}</span>
      </span>
      {c ? (
        <span className="font-medium" style={{ color: classColor(overview.classes ?? [], c.class_id) }}>
          {c.display_name}
          {c.level !== null && <span className="ml-1 text-2xs font-normal text-faint">{c.level}</span>}
        </span>
      ) : (
        <span className="text-faint">no character claimed</span>
      )}
      <span className="ml-auto text-xs text-muted">
        {c?.rank_label ? (
          <>
            {c.rank_label} {e.status === "promote" && <span className="text-gold">→ {e.target_label}</span>}
          </>
        ) : (
          <>should be {e.target_label}</>
        )}
        {e.role_name && <span className="ml-2 text-2xs text-faint">@{e.role_name}</span>}
      </span>
    </li>
  );
}

function RulesCard({ overview, onChanged }: { overview: RankSyncOverview; onChanged: () => void }) {
  const toast = useToast();
  const { roles } = useGuildLookups();
  const names = useMemo(() => new Map((overview.ranks ?? []).filter((r) => r.name).map((r) => [r.rank, r.name as string])), [overview]);
  const [roleId, setRoleId] = useState(overview.suggested_role_id ?? "");
  const [rank, setRank] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const pickable = roles.filter((r) => !r.managed && r.name !== "@everyone");
  const rankOptions = Array.from({ length: (overview.max_rank ?? 9) + 1 }, (_, i) => i);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post("/api/rank-sync/rules", { role_id: roleId, rank });
      setRank("");
      onChanged();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Couldn't save the rule");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    try {
      await api.del(`/api/rank-sync/rules/${id}`);
      onChanged();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Couldn't remove the rule");
    }
  };

  return (
    <section className="card self-start">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Rules</div>
        <p className="mt-0.5 text-2xs text-faint">Members with the role should have at least this rank in the guild.</p>
      </header>
      <ul className="divide-y divide-line">
        {(overview.rules ?? []).map((r) => (
          <li key={r.role_id} className="flex items-center gap-2 px-4 py-2 md:px-5">
            <span className="font-medium">@{r.role_name ?? "deleted role"}</span>
            <span className="text-faint">→</span>
            <span>{rankText(r.rank, names)}</span>
            <button type="button" onClick={() => remove(r.role_id)} aria-label={`Remove the rule for ${r.role_name}`} className="ml-auto rounded p-1 text-faint hover:text-crimson">
              <X className="h-3.5 w-3.5" />
            </button>
          </li>
        ))}
        {!overview.rules?.length && <li className="px-4 py-3 text-faint md:px-5">No rules yet. Add one below, e.g. Fresh → your Fresh rank.</li>}
      </ul>
      <form onSubmit={add} className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 md:px-5">
        <select className="input h-9 w-auto min-w-[8rem] flex-1 py-1 text-xs" value={roleId} onChange={(e) => setRoleId(e.target.value)} aria-label="Discord role">
          <option value="">Discord role…</option>
          {pickable.map((r) => (
            <option key={r.id} value={r.id}>
              @{r.name}
            </option>
          ))}
        </select>
        <select className="input h-9 w-auto min-w-[8rem] flex-1 py-1 text-xs" value={rank} onChange={(e) => setRank(e.target.value === "" ? "" : Number(e.target.value))} aria-label="In-game rank">
          <option value="">In-game rank…</option>
          {rankOptions.map((i) => (
            <option key={i} value={i}>
              {rankText(i, names)}
            </option>
          ))}
        </select>
        <Button type="submit" icon={<Plus className="h-3.5 w-3.5" />} loading={busy} disabled={!roleId || rank === ""}>
          Add
        </Button>
      </form>
    </section>
  );
}

function RanksCard({ overview, onChanged }: { overview: RankSyncOverview; onChanged: () => void }) {
  const toast = useToast();
  const ranks = overview.ranks ?? [];
  const [draft, setDraft] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(Object.fromEntries(ranks.map((r) => [r.rank, r.name ?? ""]))), [overview]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = ranks.some((r) => (draft[r.rank] ?? "") !== (r.name ?? ""));

  const save = async () => {
    setBusy(true);
    try {
      await api.put("/api/rank-sync/ranks", { names: Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, v])) });
      toast("ok", "Rank names saved.");
      onChanged();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : "Couldn't save the names");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card self-start">
      <header className="flex items-start justify-between gap-2 border-b border-line px-4 py-3 md:px-5">
        <div>
          <div className="label">Guild ranks</div>
          <p className="mt-0.5 text-2xs text-faint">Blizzard only gives rank numbers (0 is the guild master). Name them as in the game; the members help tell which is which.</p>
        </div>
        <Button size="sm" onClick={save} loading={busy} disabled={!dirty}>
          Save
        </Button>
      </header>
      {ranks.length === 0 ? (
        <p className="px-4 py-3 text-faint md:px-5">No ranks known yet: they come with the guild roster.</p>
      ) : (
        <ul className="divide-y divide-line">
          {ranks.map((r) => (
            <li key={r.rank} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 md:px-5">
              <span className="w-6 shrink-0 text-right font-semibold tabular-nums text-faint">{r.rank}</span>
              <input
                className="input h-8 w-40 py-1 text-xs"
                value={draft[r.rank] ?? ""}
                onChange={(e) => setDraft((d) => ({ ...d, [r.rank]: e.target.value }))}
                placeholder={r.rank === 0 ? "Guild Master" : "Rank name"}
                maxLength={40}
                aria-label={`Name of rank ${r.rank}`}
              />
              <span className="min-w-0 flex-1 truncate text-2xs text-faint">
                {r.count ? `${r.count} member${r.count === 1 ? "" : "s"}: ${r.examples.join(", ")}${r.count > r.examples.length ? ", …" : ""}` : "nobody"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function RankSyncPage() {
  const toast = useToast();
  const [overview, setOverview] = useState<RankSyncOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<RankSyncStatus>("promote");
  const [posting, setPosting] = useState(false);

  const load = useCallback(() => {
    api
      .get<RankSyncOverview>("/api/rank-sync")
      .then((r) => {
        setOverview(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load rank sync"));
  }, []);
  useEffect(load, [load]);
  const { subscribe } = useLive();
  useEffect(() => subscribe((e) => (e.type === "rank_sync.changed" || e.type === "roster.changed") && load()), [subscribe, load]);

  const post = async () => {
    setPosting(true);
    try {
      const r = await api.post<{ result: string | null }>("/api/rank-sync/post");
      toast("ok", r.result ?? "Up to date.");
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't post");
    } finally {
      setPosting(false);
    }
  };

  const entries = overview?.entries ?? [];
  const counts = Object.fromEntries(GROUPS.map((g) => [g.status, entries.filter((e) => e.status === g.status).length]));
  const shown = entries.filter((e) => e.status === tab);
  const group = GROUPS.find((g) => g.status === tab)!;

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Rank sync"
        description="Members whose Discord role comes with a higher rank in the guild than they have, so you know whom to promote in the game."
        actions={
          overview?.post ? (
            <Button icon={<Send className="h-3.5 w-3.5" />} onClick={post} loading={posting}>
              Update #{overview.post.channel_name}
            </Button>
          ) : undefined
        }
      />
      {error ? (
        <div className="card">
          <EmptyState icon={<ChevronsUp className="h-6 w-6" />} title="Rank sync isn't available">
            {error}
          </EmptyState>
        </div>
      ) : !overview ? (
        <Skeleton className="h-64" />
      ) : !overview.guild_name ? (
        <div className="card">
          <EmptyState icon={<ChevronsUp className="h-6 w-6" />} title="No guild set">
            Ranks come from your guild's roster. <Link to="/settings?group=roster#GUILD_ROSTER_GUILD" className="text-gold hover:underline">Set your guild</Link> first.
          </EmptyState>
        </div>
      ) : (
        <div className="space-y-5">
          <div className="card flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-3 text-xs text-muted md:px-5">
            <span>
              Guild <span className="text-fg">{overview.guild_name}</span>
            </span>
            {overview.post ? (
              <span>
                {overview.post.updated
                  ? `Posted in #${overview.post.channel_name}, updated ${timeAgo(new Date(overview.post.updated * 1000).toISOString())}`
                  : `Not posted in #${overview.post.channel_name} yet`}
                {overview.post.problem && <span className="ml-2 text-crimson">{overview.post.problem}</span>}
              </span>
            ) : (
              <span>
                Not posted in Discord.{" "}
                <Link to="/settings?group=rank_sync#RANK_SYNC_CHANNEL_ID" className="text-gold hover:underline">
                  Pick a channel
                </Link>
              </span>
            )}
            <span className="text-faint">Ranks update with the guild roster (Blizzard's can be up to 3 hours old).</span>
          </div>

          <section className="card">
            <div className="flex flex-wrap gap-1.5 border-b border-line px-4 py-3 md:px-5" role="group" aria-label="Group">
              {GROUPS.map((g) => (
                <button
                  key={g.status}
                  type="button"
                  aria-pressed={tab === g.status}
                  onClick={() => setTab(g.status)}
                  className={cx("rounded-md px-2.5 py-1.5 text-xs font-medium transition", tab === g.status ? "bg-gold/15 text-gold" : "text-muted hover:text-fg")}
                >
                  {g.label} <Badge tone={g.status === "promote" && counts[g.status] ? "gold" : "neutral"}>{counts[g.status]}</Badge>
                </button>
              ))}
            </div>
            <p className="border-b border-line px-4 py-2 text-2xs text-faint md:px-5">{group.hint}</p>
            {!overview.rules?.length ? (
              <p className="px-4 py-3 text-faint md:px-5">Add a rule first (below), e.g. Fresh → your Fresh rank.</p>
            ) : shown.length === 0 ? (
              <p className="px-4 py-3 text-faint md:px-5">{tab === "promote" ? "Nobody to promote right now." : "Nobody here."}</p>
            ) : (
              <ul className="divide-y divide-line">
                {shown.map((e) => (
                  <EntryRow key={e.member.id} e={e} overview={overview} />
                ))}
              </ul>
            )}
          </section>

          <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
            <RulesCard overview={overview} onChanged={load} />
            <RanksCard overview={overview} onChanged={load} />
          </div>
        </div>
      )}
    </>
  );
}
