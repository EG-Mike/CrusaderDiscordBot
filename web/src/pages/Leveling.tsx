// Leveling: the leaderboard, recent level-ups (and whether they were
// announced), and where the milestone announcements go.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Hash, MessageSquareText, RefreshCw, Trophy } from "lucide-react";
import { ApiError, api, type LevelingOverview, type RosterOverview, type WowClass } from "../lib/api";
import { useLive } from "../lib/live";
import { timeAgo } from "../lib/time";
import { className } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { CharacterName } from "../components/CharacterName";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Segmented, Skeleton } from "../components/ui";

type Scope = "mains" | "all";

const OUTCOMES: Record<string, { label: string; tone: "ok" | "neutral" | "warn" }> = {
  announced: { label: "Announced", tone: "ok" },
  alt: { label: "Alt, not announced", tone: "neutral" },
  "too old": { label: "Too late to announce", tone: "neutral" },
  "no channel": { label: "No channel set", tone: "warn" },
  "no milestone": { label: "No milestone", tone: "neutral" },
};

export default function LevelingPage() {
  const toast = useToast();
  const { subscribe } = useLive();
  const [data, setData] = useState<LevelingOverview | null>(null);
  const [classes, setClasses] = useState<WowClass[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<Scope>("mains");
  const [checking, setChecking] = useState(false);

  const load = useCallback(() => {
    api
      .get<LevelingOverview>("/api/leveling")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load leveling"));
  }, []);
  useEffect(() => {
    load();
    api.get<RosterOverview>("/api/roster").then((d) => setClasses(d.classes)).catch(() => undefined);
    return subscribe((event) => event.type === "roster.changed" && load());
  }, [load, subscribe]);

  const check = async () => {
    setChecking(true);
    try {
      const r = await api.post<{ checked: number | null; level_ups: number; announced: number }>("/api/roster/sync");
      toast("ok", `Checked${r.checked !== null ? ` ${r.checked} characters` : ""}: ${r.level_ups} level-up${r.level_ups === 1 ? "" : "s"}, ${r.announced} announced.`);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "The check didn't work");
    } finally {
      setChecking(false);
    }
  };

  const board = useMemo(() => {
    const rows = data?.leaderboard ?? [];
    return (scope === "mains" ? rows.filter((c) => !c.discord_id || c.is_main) : rows).slice(0, 25);
  }, [data, scope]);

  if (error) return <EmptyState title="Couldn't load leveling">{error}</EmptyState>;

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Leveling"
        description="The guild's highest levels, and the level-ups the bot congratulated. Levels update when a player logs out."
        actions={
          <Button icon={<RefreshCw className="h-3.5 w-3.5" />} loading={checking} onClick={check}>
            Check now
          </Button>
        }
      />

      {data && !data.enabled && (
        <div className="mb-5 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3 text-muted">
          <span className="font-medium text-warn">Leveling is switched off.</span>{" "}
          <Link to="/settings?group=features#FEATURE_LEVELING_ENABLED" className="text-gold hover:underline">
            Turn it on
          </Link>
        </div>
      )}

      <section className="card mb-5 flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 md:px-5">
        <span className="inline-flex items-center gap-1.5">
          <Hash className="h-3.5 w-3.5 text-faint" />
          {data ? (
            data.channel ? (
              <>Announced in #{data.channel.name}</>
            ) : (
              <span className="text-warn">No announcement channel</span>
            )
          ) : (
            "…"
          )}
        </span>
        <span className="inline-flex flex-wrap items-center gap-1">
          <span className="text-faint">Milestones</span>
          {data?.milestones.map((m) => (
            <Badge key={m} tone="gold">
              {m}
            </Badge>
          ))}
        </span>
        <span className="text-faint">{data ? (data.announce_alts ? "Alts are announced too" : "Mains only") : ""}</span>
        <span className="flex-1" />
        <Link to="/settings?group=leveling" className="text-xs text-gold hover:underline">
          Change in Settings
        </Link>
        <Link to="/bot-messages?key=level_milestone" className="inline-flex items-center gap-1 text-xs text-gold hover:underline">
          <MessageSquareText className="h-3 w-3" /> Edit the message
        </Link>
      </section>

      <div className="grid gap-5 lg:grid-cols-2">
        <section className="card">
          <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3 md:px-5">
            <div className="label">Leaderboard</div>
            <Segmented value={scope} onChange={setScope} label="Characters" options={[["mains", "Mains"], ["all", "Everyone"]] as const} />
          </header>
          {!data ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-8" />
              ))}
            </div>
          ) : board.length === 0 ? (
            <EmptyState icon={<Trophy className="h-6 w-6" />} title="Nobody on the roster yet">
              Set the guild name in Settings, then Check now.
            </EmptyState>
          ) : (
            <ol className="divide-y divide-line">
              {board.map((c, i) => (
                <li key={c.slug} className="flex items-center gap-3 px-4 py-2 md:px-5">
                  <span className={i < 3 ? "w-6 text-right font-semibold text-gold tabular-nums" : "w-6 text-right text-faint tabular-nums"}>{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <CharacterName c={c} classes={classes} />
                    <div className="truncate text-2xs text-faint">
                      {className(classes, c.class_id)}
                      {c.member && ` · ${c.member.name}`}
                    </div>
                  </div>
                  <span className="text-lg font-semibold tabular-nums">{c.level}</span>
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="card">
          <header className="border-b border-line px-4 py-3 md:px-5">
            <div className="label">Recent level-ups</div>
          </header>
          {!data ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          ) : data.events.length === 0 ? (
            <EmptyState title="No level-ups yet">They appear here as characters level up (the first look at a character only records its level).</EmptyState>
          ) : (
            <ul className="divide-y divide-line">
              {data.events.map((e) => {
                const outcome = e.outcome ? OUTCOMES[e.outcome] : null;
                return (
                  <li key={e.id} className="flex items-center gap-3 px-4 py-2.5 md:px-5">
                    {e.member ? <Avatar url={e.member.avatar_url} name={e.member.name} /> : <Avatar url={null} name={e.display_name} />}
                    <div className="min-w-0 flex-1">
                      <div className="truncate">
                        <CharacterName c={e} classes={classes} /> <span className="text-muted">reached level</span>{" "}
                        <span className="font-semibold">{e.new_level}</span>
                        {e.old_level !== null && <span className="text-faint"> (from {e.old_level})</span>}
                      </div>
                      <div className="text-2xs text-faint">{timeAgo(e.created_at)}</div>
                    </div>
                    {e.milestone && !outcome && <Badge tone="info">Waiting</Badge>}
                    {outcome && e.outcome !== "no milestone" && <Badge tone={outcome.tone}>{outcome.label}</Badge>}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
