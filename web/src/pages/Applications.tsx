// Applications (webapi/applications_api.py): the same applications as the
// review channel, with the character read from Blizzard. Moderators read them;
// moderators and approvers decide (the server checks it, these buttons only follow).
// Decisions run the bot's own rules: roles, nickname, DM, roster link, and
// the card in Discord updates too.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  ArrowLeft,
  Check,
  ClipboardList,
  ExternalLink,
  Image as ImageIcon,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  X,
} from "lucide-react";
import { ApiError, api, type ApplicationDetail, type ApplicationList, type ApplicationRow, type ApplicationStatus } from "../lib/api";
import { useLive } from "../lib/live";
import { fullDateTime, timeAgo, useNow } from "../lib/time";
import { CLASS_COLORS } from "../lib/wow";
import { Avatar } from "../components/Audit";
import { GearCard, ProfessionsCard, StatsCard, TalentsCard } from "../components/Gear";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Segmented, Skeleton, StatTile } from "../components/ui";

type Tab = ApplicationStatus | "all";

const STATUS_BADGE: Record<ApplicationStatus, { tone: "warn" | "ok" | "crimson"; label: string }> = {
  pending: { tone: "warn", label: "Pending" },
  approved: { tone: "ok", label: "Approved" },
  denied: { tone: "crimson", label: "Denied" },
};

const iso = (seconds: number) => new Date(seconds * 1000).toISOString();

function StatusBadge({ status }: { status: ApplicationStatus }) {
  const meta = STATUS_BADGE[status];
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

function specLine(a: Pick<ApplicationRow, "spec" | "role" | "offspec" | "class_name">) {
  const main = [a.spec, a.class_name].filter(Boolean).join(" ");
  return [main || "Unknown class", a.role, a.offspec ? `offspec ${a.offspec}` : null].filter(Boolean).join(" · ");
}

function useApplicationsLive(load: () => void, id?: string) {
  const { subscribe } = useLive();
  useEffect(
    () =>
      subscribe((e) => {
        if (e.type === "applications.changed" && (!id || e.id === id)) load();
      }),
    [subscribe, load, id],
  );
}

// --- the list ----------------------------------------------------------------------------------

function Row({ a, now }: { a: ApplicationRow; now: number }) {
  return (
    <li>
      <Link to={`/applications/${a.id}`} className="flex items-center gap-3 px-4 py-3 transition hover:bg-raised/60 md:px-5">
        <Avatar url={a.applicant.avatar_url} name={a.applicant.name ?? a.character} size="h-8 w-8" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="truncate font-semibold" style={{ color: CLASS_COLORS[a.class_name ?? ""] }}>
              {a.character}
            </span>
            <StatusBadge status={a.status} />
            {a.prior_denials >= 2 && <Badge tone="crimson">Denied {a.prior_denials}× before</Badge>}
            {a.archived && <Badge>Archived</Badge>}
          </div>
          <div className="truncate text-2xs text-muted">
            {specLine(a)}
            {a.applicant.name ? ` · by ${a.applicant.name}` : ""}
            {!a.applicant_in_server && " (left the server)"}
          </div>
        </div>
        <div className="hidden shrink-0 items-center gap-3 text-2xs text-muted sm:flex">
          {a.flag_count > 0 && (
            <span className="inline-flex items-center gap-1 text-warn" title="Warnings from the character check">
              <AlertTriangle className="h-3.5 w-3.5" /> {a.flag_count}
            </span>
          )}
          {a.has_note && <MessageSquare className="h-3.5 w-3.5" aria-label="Has a note" />}
          {a.screenshot_count > 0 && <ImageIcon className="h-3.5 w-3.5" aria-label="Has screenshots" />}
        </div>
        <div className="w-16 shrink-0 text-right">
          <div className="font-semibold tabular-nums">{a.item_level ?? "–"}</div>
          <div className="text-2xs text-faint">ilvl</div>
        </div>
        <div className="hidden w-20 shrink-0 text-right text-2xs text-faint md:block" title={fullDateTime(iso(a.submitted_at))}>
          {timeAgo(iso(a.submitted_at), now)}
        </div>
      </Link>
    </li>
  );
}

export function ApplicationsPage() {
  const toast = useToast();
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab | null) ?? "pending";
  const [query, setQuery] = useState("");
  const [data, setData] = useState<ApplicationList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const load = useCallback(() => {
    api.get<ApplicationList>("/api/applications").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load applications"));
  }, []);
  useEffect(load, [load]);
  useApplicationsLive(load);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (data?.applications ?? []).filter(
      (a) => (tab === "all" || a.status === tab) && (!q || [a.character, a.applicant.name, a.applicant.username].some((v) => v?.toLowerCase().includes(q))),
    );
  }, [data, tab, query]);

  const decided = (data?.counts.approved ?? 0) + (data?.counts.denied ?? 0);

  const archive = async () => {
    setArchiving(true);
    try {
      const r = await api.post<{ moved: number; failed: number }>("/api/applications/archive");
      toast(r.failed ? "error" : "ok", `Archived ${r.moved} application${r.moved === 1 ? "" : "s"}${r.failed ? `, ${r.failed} failed (see the System log)` : ""}.`);
      setArchiveOpen(false);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Archiving didn't work");
    } finally {
      setArchiving(false);
    }
  };

  return (
    <>
      <SectionHeader
        eyebrow="Members"
        title="Applications"
        description="New members apply in Discord; the bot reads their character from Blizzard. Decide here or on the card in the review channel."
        actions={
          <Button icon={<Archive className="h-3.5 w-3.5" />} onClick={() => setArchiveOpen(true)} disabled={!data || Boolean(data.archive_problem) || decided === 0} title={data?.archive_problem ?? undefined}>
            Archive decided
          </Button>
        }
      />

      <div className="mb-5 grid grid-cols-3 gap-3">
        <StatTile value={data?.counts.pending ?? "–"} label="Waiting for a decision" tone={data?.counts.pending ? "warn" : undefined} />
        <StatTile value={data?.counts.approved ?? "–"} label="Approved" />
        <StatTile value={data?.counts.denied ?? "–"} label="Denied" />
      </div>

      {data && !data.can_decide && (
        <p className="mb-4 rounded-lg border border-line bg-raised/40 px-3 py-2 text-xs text-muted">
          You can read applications. Approving and denying needs the moderator or approver role (Settings, Applications).
        </p>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented
          label="Status"
          value={tab}
          onChange={(t) => setParams(t === "pending" ? {} : { tab: t })}
          options={[
            ["pending", `Pending${data ? ` (${data.counts.pending})` : ""}`],
            ["approved", "Approved"],
            ["denied", "Denied"],
            ["all", "All"],
          ]}
        />
        <div className="relative min-w-[12rem] flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <input className="input h-9 pl-9" placeholder="Character or member" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search applications" />
        </div>
      </div>

      <section className="card">
        {error ? (
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load applications">
            {error}
          </EmptyState>
        ) : !data ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : shown.length === 0 ? (
          <EmptyState icon={<ClipboardList className="h-6 w-6" />} title={tab === "pending" && !query ? "Nothing waiting" : "No applications"}>
            {tab === "pending" && !query ? "New applications show up here as soon as they're posted in the review channel." : "Nothing matches these filters."}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {shown.map((a) => (
              <Row key={a.id} a={a} now={now} />
            ))}
          </ul>
        )}
      </section>

      <Dialog
        open={archiveOpen}
        onClose={() => setArchiveOpen(false)}
        title="Archive decided applications"
        busy={archiving}
        actions={
          <>
            <Button variant="ghost" onClick={() => setArchiveOpen(false)} disabled={archiving}>
              Cancel
            </Button>
            <Button variant="primary" loading={archiving} onClick={archive} icon={<Archive className="h-3.5 w-3.5" />}>
              Archive {decided}
            </Button>
          </>
        }
      >
        <p className="text-muted">
          Moves every approved and denied card from the review channel to the archive channel, so the review channel only shows what's still pending. They stay
          listed here.
        </p>
      </Dialog>
    </>
  );
}

// --- one application ---------------------------------------------------------------------------

function DecisionPanel({ a, canDecide, onDone }: { a: ApplicationDetail; canDecide: boolean; onDone: (a: ApplicationDetail) => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"approve" | "deny" | null>(null);
  const [reason, setReason] = useState("");

  const act = async (action: "approve" | "deny" | "reset" | "refresh-gear", body?: object) => {
    setBusy(action);
    try {
      const updated = await api.post<ApplicationDetail>(`/api/applications/${a.id}/${action}`, body);
      onDone(updated);
      setDialog(null);
      setReason("");
      toast(
        "ok",
        {
          approve: `${a.character} is approved.`,
          deny: `${a.character} is denied.`,
          reset: "Back to pending.",
          "refresh-gear": "Refreshed from Blizzard.",
        }[action],
      );
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That didn't work");
    } finally {
      setBusy(null);
    }
  };

  const d = a.decision;
  const noDecideHint = canDecide ? undefined : "Needs the moderator or approver role";
  return (
    <section className="card px-4 py-4 md:px-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="label">Decision</div>
        <StatusBadge status={a.status} />
      </div>

      {a.status === "pending" ? (
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" icon={<Check className="h-3.5 w-3.5" />} disabled={!canDecide || busy !== null} title={noDecideHint} onClick={() => setDialog("approve")}>
            Approve
          </Button>
          <Button variant="danger" icon={<X className="h-3.5 w-3.5" />} disabled={!canDecide || busy !== null} title={noDecideHint} onClick={() => setDialog("deny")}>
            Deny
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          {d ? (
            <div className="space-y-1 text-[13px]">
              <p>
                {d.status === "approved" ? "Approved" : "Denied"} by <span className="font-medium">{d.by_name}</span>
                {d.via === "dashboard" ? " on the dashboard" : " in Discord"} · <span className="text-muted">{fullDateTime(iso(d.at))}</span>
              </p>
              {d.status === "approved" && (
                <>
                  <p className="text-muted">Roles given: {d.roles_assigned.length ? d.roles_assigned.map(stripEmoji).join(", ") : "none"}</p>
                  {d.roles_kept.length > 0 && <p className="text-muted">Already had: {d.roles_kept.map(stripEmoji).join(", ")}</p>}
                  {d.nickname && <p className="text-muted">{d.nickname}</p>}
                  {d.roster && <p className="text-muted">{d.roster}</p>}
                </>
              )}
              {d.reason && (
                <p className="text-muted">
                  Reason (reviewers only): <span className="text-fg">{d.reason}</span>
                </p>
              )}
            </div>
          ) : (
            a.decision_text && <p className="whitespace-pre-line text-[13px] text-muted">{stripEmoji(a.decision_text)}</p>
          )}
          <Button size="sm" icon={<RotateCcw className="h-3.5 w-3.5" />} disabled={!canDecide || busy !== null} title={noDecideHint} loading={busy === "reset"} onClick={() => act("reset")}>
            Reset to pending
          </Button>
        </div>
      )}

      <Dialog
        open={dialog === "approve"}
        onClose={() => setDialog(null)}
        title={`Approve ${a.character}?`}
        busy={busy !== null}
        actions={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)} disabled={busy !== null}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy === "approve"} onClick={() => act("approve")} icon={<Check className="h-3.5 w-3.5" />}>
              Approve
            </Button>
          </>
        }
      >
        <ul className="list-disc space-y-1 pl-5 text-muted">
          <li>Gives the approval role (unless they already have an exempt role) and their class and role roles.</li>
          <li>Changes their server nickname to {a.character}.</li>
          <li>Links the character to them on the guild roster, with their specs.</li>
          <li>Sends them the approval DM, and updates the card in the review channel.</li>
        </ul>
      </Dialog>

      <Dialog
        open={dialog === "deny"}
        onClose={() => setDialog(null)}
        title={`Deny ${a.character}?`}
        busy={busy !== null}
        actions={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)} disabled={busy !== null}>
              Cancel
            </Button>
            <Button variant="danger" loading={busy === "deny"} onClick={() => act("deny", { reason: reason.trim() || null })} icon={<X className="h-3.5 w-3.5" />}>
              Deny
            </Button>
          </>
        }
      >
        <label className="label mb-1 block" htmlFor="deny-reason">
          Reason (optional)
        </label>
        <textarea id="deny-reason" className="input min-h-[5rem] text-[13px]" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        <p className="mt-2 text-2xs text-faint">
          Only reviewers see the reason; the applicant never does. A first denial sends them the denial DM. Denying after an approval only takes the approval role
          away.
        </p>
      </Dialog>
      <RefreshGear a={a} busy={busy} act={act} />
    </section>
  );
}

function RefreshGear({ a, busy, act }: { a: ApplicationDetail; busy: string | null; act: (action: "refresh-gear") => void }) {
  if (!a.character_slug) return null;
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line pt-3">
      <Button size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} loading={busy === "refresh-gear"} disabled={busy !== null} onClick={() => act("refresh-gear")}>
        Refresh from Blizzard
      </Button>
      <span className="text-2xs text-faint">
        Blizzard updates characters when the player logs out.{a.gear_checked_at ? ` Last read ${timeAgo(iso(a.gear_checked_at))}.` : ""}
      </span>
    </div>
  );
}

/** Discord custom emoji (<:name:id>) don't render here; keep the words. */
function stripEmoji(text: string) {
  return text.replace(/<a?:[\w~]+:\d+>\s?/g, "").trim();
}

export function ApplicationPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [a, setA] = useState<ApplicationDetail | null>(null);
  const [canDecide, setCanDecide] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .get<ApplicationDetail>(`/api/applications/${id}`)
      .then((r) => {
        setA(r);
        setError(null);
        if (r.id !== id) navigate(`/applications/${r.id}`, { replace: true }); // it was archived since
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the application"));
    api
      .get<ApplicationList>("/api/applications")
      .then((l) => setCanDecide(l.can_decide))
      .catch(() => undefined);
  }, [id, navigate]);
  useEffect(load, [load]);
  useApplicationsLive(load, id);

  const back = (
    <Link to="/applications" className="mb-3 inline-flex items-center gap-1 text-xs text-muted hover:text-fg">
      <ArrowLeft className="h-3.5 w-3.5" /> Applications
    </Link>
  );

  if (error)
    return (
      <>
        {back}
        <div className="card">
          <EmptyState icon={<ClipboardList className="h-6 w-6" />} title="Not found">
            {error}
          </EmptyState>
        </div>
      </>
    );
  if (!a)
    return (
      <>
        {back}
        <Skeleton className="h-64" />
      </>
    );

  const g = a.gear;
  const warnings = [...(a.requirement_problems.length ? [`Below the guild's minimum: ${a.requirement_problems.join(", ")}`] : []), ...a.flags];

  return (
    <>
      {back}
      <section className="card mb-5 flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:gap-6 md:px-5" style={{ borderLeft: `3px solid ${CLASS_COLORS[a.class_name ?? ""] ?? "transparent"}` }}>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold" style={{ color: CLASS_COLORS[a.class_name ?? ""] }}>
              {a.character}
            </h1>
            <StatusBadge status={a.status} />
            {a.archived && <Badge>Archived</Badge>}
          </div>
          <p className="text-muted">
            {g ? `Level ${g.level ?? "?"} ${[g.race, a.class_name].filter(Boolean).join(" ")}` : a.class_name ?? "Unknown class"}
            {g?.guild ? ` · <${g.guild}>` : g ? " · no guild" : ""}
            {g?.ruleset ? ` · ${g.ruleset} ruleset` : ""}
          </p>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 text-2xs text-faint">
            <span className="inline-flex items-center gap-1.5">
              <Avatar url={a.applicant.avatar_url} name={a.applicant.name} size="h-4 w-4" />
              {a.applicant.name ?? `Member ${a.applicant.id}`}
              {a.applicant.username && a.applicant.username !== a.applicant.name ? ` (@${a.applicant.username})` : ""}
              {!a.applicant_in_server && " · not in the server any more"}
            </span>
            <span>· applied {fullDateTime(iso(a.submitted_at))}</span>
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {a.message_url && (
            <a href={a.message_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-xs transition hover:bg-raised">
              <ExternalLink className="h-3.5 w-3.5" /> Card in Discord
            </a>
          )}
          {a.profile_url && (
            <a href={a.profile_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-xs transition hover:bg-raised">
              <ExternalLink className="h-3.5 w-3.5" /> Profile
            </a>
          )}
          {a.character_slug && (
            <Link to={`/lookup?name=${encodeURIComponent(a.character)}`} className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-xs transition hover:bg-raised">
              <Search className="h-3.5 w-3.5" /> Look up live
            </Link>
          )}
        </div>
      </section>

      {a.prior_denials >= 2 && (
        <p className="mb-5 flex items-start gap-2 rounded-xl border border-crimson/30 bg-crimson/[0.06] px-4 py-3 text-crimson">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> This member was denied {a.prior_denials} times before.
        </p>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile value={g?.equipped_item_level ?? "?"} label="Item level (equipped)" icon={<Shield className="h-3 w-3" />} />
        <StatTile value={g?.level ?? "?"} label="Level" />
        <StatTile value={<span className="text-lg">{a.spec ?? "?"}</span>} label={`Main spec${a.role ? ` · ${a.role}` : ""}`} />
        <StatTile value={<span className="text-lg">{a.offspec ?? "None"}</span>} label="Offspec" />
      </div>

      {warnings.length > 0 && (
        <div className="mb-5 space-y-1 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          {warnings.map((f) => (
            <p key={f} className="flex items-start gap-2 text-muted">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" /> {f}
            </p>
          ))}
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="space-y-5">
          {g ? (
            <GearCard items={g.items} />
          ) : (
            <section className="card px-4 py-4 md:px-5">
              <div className="label mb-1">Gear</div>
              {a.legacy_text ? (
                <p className="whitespace-pre-line text-[13px] text-muted">{stripEmoji(a.legacy_text.replace(/\*\*/g, ""))}</p>
              ) : (
                <p className="text-faint">Blizzard had no data for this character when they applied (below level 10, or the name didn't match). Check by hand.</p>
              )}
            </section>
          )}

          <section className="card px-4 py-4 md:px-5">
            <div className="label mb-2">Applicant's note</div>
            {a.note ? <p className="whitespace-pre-line text-[13px]">{a.note}</p> : <p className="text-faint">No note.</p>}
            {a.screenshots.length > 0 && (
              <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
                {a.screenshots.map((url, i) => (
                  <a key={url} href={url} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-lg ring-1 ring-line">
                    <img src={url} alt={`Screenshot ${i + 1}`} className="aspect-video w-full object-cover transition hover:opacity-90" loading="lazy" />
                  </a>
                ))}
              </div>
            )}
          </section>
        </div>

        <div className="space-y-5">
          <DecisionPanel a={a} canDecide={canDecide} onDone={setA} />
          {g && g.stat_highlights.length > 0 && (
            <section className="card px-4 py-3 md:px-5">
              <div className="label mb-2">Key stats for {a.role ?? "their role"}</div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {g.stat_highlights.map((s) => (
                  <div key={s.key} className="rounded-lg border border-line bg-raised/40 px-2.5 py-1.5">
                    <div className="font-semibold tabular-nums">{s.text}</div>
                    <div className="truncate text-2xs text-muted">{s.label}</div>
                  </div>
                ))}
              </div>
            </section>
          )}
          {g && <TalentsCard talents={g.talents} />}
          {g && <StatsCard groups={g.stat_groups} />}
          {g && <ProfessionsCard professions={g.professions} />}
          <section className="card px-4 py-3 md:px-5">
            <div className="label mb-1">WarcraftLogs</div>
            {a.warcraftlogs.text ? (
              <p className="whitespace-pre-line text-[13px] text-muted">{a.warcraftlogs.text.replace(/\*\*/g, "")}</p>
            ) : (
              <p className="text-faint">
                {a.warcraftlogs.enabled ? "No parses found for this character." : "Parses show here once WarcraftLogs covers WoW Forever (Settings, Applications)."}
              </p>
            )}
          </section>
        </div>
      </div>
    </>
  );
}
