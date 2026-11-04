import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  Clock,
  Power,
  RotateCcw,
  GitCommitHorizontal,
  ScrollText,
  Skull,
  ToggleRight,
} from "lucide-react";
import { api, type AuditEvent, type AuditPage, type Me } from "../lib/api";
import { formatDuration, useLive } from "../lib/live";
import { fullDateTime, timeAgo, useNow } from "../lib/time";
import { Badge, EmptyState, Skeleton, StatTile, cx } from "../components/ui";
import { RestartButton } from "../components/Restart";
import { Avatar, KindBadge, Where } from "../components/Audit";

const FEED_SIZE = 8;

/** The latest audit entries, updated live. */
function ActivityFeed({ className }: { className?: string }) {
  const { subscribe } = useLive();
  const now = useNow();
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [error, setError] = useState(false);
  const [fresh, setFresh] = useState<number | null>(null);

  useEffect(() => {
    api
      .get<AuditPage>(`/api/audit?limit=${FEED_SIZE}`)
      .then((page) => setEvents(page.events))
      .catch(() => setError(true));
    return subscribe((message) => {
      if (message.type !== "audit.event") return;
      const event = message.event as AuditEvent;
      setEvents((current) => (current && !current.some((e) => e.id === event.id) ? [event, ...current].slice(0, FEED_SIZE) : current));
      setFresh(event.id);
    });
  }, [subscribe]);

  return (
    <section className={cx("card min-w-0", className)}>
      <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <div className="flex items-center gap-2">
          <div className="label">Recent activity</div>
          <span className="relative flex h-1.5 w-1.5" title="Updates live">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-gold opacity-40" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-gold" />
          </span>
        </div>
        <Link to="/audit-log" className="text-xs text-gold hover:underline">
          Open audit log
        </Link>
      </header>
      {error ? (
        <div className="px-5 py-6 text-muted">Couldn't load recent activity.</div>
      ) : events === null ? (
        <div className="space-y-2 p-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-8" />
          ))}
        </div>
      ) : events.length === 0 ? (
        <EmptyState icon={<ScrollText className="h-6 w-6" />} title="No activity yet">
          Commands, buttons and dashboard changes will show up here as they happen.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {events.map((event) => (
            <li key={event.id}>
              <Link
                to={event.actor_id ? `/audit-log?actor=${event.actor_id}` : "/audit-log"}
                className={cx(
                  "flex items-center gap-3 px-5 py-2.5 transition-colors duration-1000 hover:bg-raised/50",
                  fresh === event.id && "bg-gold/[0.06]",
                )}
                title={`${fullDateTime(event.created_at)} · show everything by ${event.actor_name ?? "this member"}`}
              >
                <Avatar url={event.actor_avatar_url} name={event.actor_name} />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="shrink-0 font-medium">{event.actor_name ?? "Unknown"}</span>
                    <span className="truncate text-muted">{event.summary}</span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-2xs">
                    <KindBadge kind={event.kind} />
                    <Where event={event} />
                  </div>
                </div>
                <time dateTime={event.created_at} className="shrink-0 text-2xs tabular-nums text-faint">
                  {timeAgo(event.created_at, now)}
                </time>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}


function greeting() {
  const h = new Date().getHours();
  return h < 6 ? "Late night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export default function Overview({ me }: { me: Me }) {
  const { status } = useLive();

  if (!status)
    return (
      <div className="space-y-4">
        <Skeleton className="h-28" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-64" />
      </div>
    );

  const features = Object.entries(status.features);
  const enabled = features.filter(([, on]) => on).length;
  const missing = status.missing_setup;
  const isAdmin = me.levels.includes("admin");
  // Console errors are admin-only (see the System log page); unexpected
  // stops are shown to every moderator.
  const errors = isAdmin ? status.system.errors_24h : 0;
  const crashes = status.system.crashes_7d;
  const attention =
    status.warnings.length +
    (status.pending_restart.length ? 1 : 0) +
    (missing.length ? 1 : 0) +
    (errors ? 1 : 0) +
    (crashes ? 1 : 0);
  const lastUpdate = status.system.last_update;

  return (
    <>
      <div className="card relative mb-5 overflow-hidden p-5 md:p-6">
        <div className="pointer-events-none absolute -right-16 -top-24 h-64 w-64 rounded-full bg-gold/10 blur-3xl" />
        <div className="label mb-2 text-gold/90">● Overview</div>
        <h1 className="text-xl font-semibold tracking-tight">
          {greeting()}, {me.username}
        </h1>
        <p className="mt-1 max-w-2xl text-muted">
          {status.bot.name ?? "The bot"} is {status.online ? "online" : "offline"} in{" "}
          <span className="text-fg">{status.guild?.name ?? "your server"}</span>.{" "}
          {attention ? `${attention} thing${attention === 1 ? " needs" : "s need"} your attention.` : "Everything looks good."}
        </p>
        <p className="mt-2 flex flex-wrap items-center gap-x-1.5 text-xs text-faint">
          <GitCommitHorizontal className="h-3.5 w-3.5" />
          Running version{" "}
          {status.version_info.commit_url ? (
            <a href={status.version_info.commit_url} target="_blank" rel="noreferrer" className="font-mono text-muted hover:text-gold">
              {status.version}
            </a>
          ) : (
            <span className="font-mono text-muted">{status.version}</span>
          )}
          {status.version_info.title && <span className="max-w-full truncate">· {status.version_info.title}</span>}
          <span>
            ·{" "}
            <Link to="/system-log?view=lifecycle" className="hover:text-gold">
              {lastUpdate ? `updated ${timeAgo(lastUpdate.at)}` : "no updates recorded yet"}
            </Link>
          </span>
        </p>
      </div>

      {missing.length > 0 && (
        <section className="card mb-5 border-gold/30 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="label mb-1.5 text-gold/90">● Finish setup</div>
              <h2 className="text-base font-semibold">
                {missing.length} thing{missing.length === 1 ? "" : "s"} to set before every feature works
              </h2>
              <p className="mt-1 text-muted">The setup guide walks you through them; it only takes a few minutes.</p>
            </div>
            <Link
              to="/setup"
              className="inline-flex h-9 items-center rounded-lg border border-gold/50 bg-gold/15 px-3.5 font-medium text-gold transition hover:bg-gold/25"
            >
              {status.setup_completed ? "Open setup" : "Continue setup"}
            </Link>
          </div>
          <ul className="mt-4 grid gap-2 sm:grid-cols-2">
            {missing.map((m) => (
              <li key={m.key} className="min-w-0">
                <Link
                  to={`/settings?group=${m.group}#${m.key}`}
                  className="flex items-center gap-2.5 rounded-lg border border-line bg-bg/50 px-3 py-2 transition hover:border-gold/40"
                >
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gold" />
                  <span className="min-w-0 flex-1 truncate">{m.label}</span>
                  <span className="font-mono text-2xs text-faint">{m.key}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          value={status.online ? status.latency_ms ?? "—" : "Off"}
          label={status.online ? "ms latency" : "Bot offline"}
          icon={<Activity className="h-3 w-3" />}
          tone={status.online ? "ok" : "crimson"}
        />
        <StatTile value={formatDuration(status.uptime_seconds)} label="Uptime" icon={<Clock className="h-3 w-3" />} />
        <StatTile
          value={status.counts.pending_applications}
          label="Pending applications"
          icon={<ClipboardList className="h-3 w-3" />}
          tone={status.counts.pending_applications ? "gold" : undefined}
        />
        <StatTile
          value={`${enabled}/${features.length}`}
          label="Features on"
          icon={<ToggleRight className="h-3 w-3" />}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-5">
        <section className="card lg:col-span-3">
          <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
            <div>
              <div className="label">Needs attention</div>
            </div>
            {attention === 0 && <Badge tone="ok">All clear</Badge>}
          </header>
          <div className="divide-y divide-line">
            {status.pending_restart.length > 0 && (
              <div className="flex gap-3 px-5 py-3.5">
                <RotateCcw className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">
                    {status.pending_restart.length} setting change{status.pending_restart.length === 1 ? "" : "s"} waiting for a restart
                  </div>
                  <p className="mt-0.5 text-muted">
                    Restart the bot to apply them:{" "}
                    <span className="font-mono text-2xs text-faint">{status.pending_restart.join(", ")}</span>
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-3 self-center">
                  <Link to="/settings?filter=pending" className="text-xs text-gold hover:underline">
                    Review
                  </Link>
                  <RestartButton />
                </div>
              </div>
            )}
            {crashes > 0 && (
              <div className="flex gap-3 px-5 py-3.5">
                <Skull className="mt-0.5 h-4 w-4 shrink-0 text-crimson" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">
                    The bot stopped unexpectedly {crashes === 1 ? "once" : `${crashes} times`} in the last 7 days
                  </div>
                  <p className="mt-0.5 text-muted">A crash, running out of memory, or the server restarting without warning.</p>
                </div>
                <Link to="/system-log?view=lifecycle&range=7d" className="shrink-0 self-center text-xs text-gold hover:underline">
                  Review
                </Link>
              </div>
            )}
            {errors > 0 && (
              <div className="flex gap-3 px-5 py-3.5">
                <AlertOctagon className="mt-0.5 h-4 w-4 shrink-0 text-crimson" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">
                    {errors} error{errors === 1 ? "" : "s"} in the bot's console in the last 24 hours
                  </div>
                  <p className="mt-0.5 text-muted">
                    Latest {status.system.last_error_at ? timeAgo(status.system.last_error_at) : "recently"}. Read them,
                    with tracebacks, in the System log.
                  </p>
                </div>
                <Link to="/system-log?view=errors&range=24h" className="shrink-0 self-center text-xs text-gold hover:underline">
                  Review
                </Link>
              </div>
            )}
            {status.warnings.map((warning) => (
              <div key={warning} className="flex gap-3 px-5 py-3.5">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
                <div className="text-muted">{warning}</div>
              </div>
            ))}
            {attention === 0 && (
              <div className="flex items-center gap-3 px-5 py-6 text-muted">
                <CheckCircle2 className="h-4 w-4 text-ok" /> No warnings. The configuration matches what the bot is running.
              </div>
            )}
          </div>
        </section>

        <section className="card lg:col-span-2">
          <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
            <div className="label">Features</div>
            <Link to="/settings?group=features" className="text-xs text-gold hover:underline">
              Manage
            </Link>
          </header>
          <ul className="grid grid-cols-1 gap-px p-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
            {features.map(([key, on]) => (
              <li key={key} className="flex items-center gap-2.5 rounded-lg px-3 py-2">
                <Power className={cx("h-3.5 w-3.5", on ? "text-ok" : "text-faint")} />
                <span className={on ? "" : "text-faint"}>{status.feature_labels[key] ?? key}</span>
              </li>
            ))}
          </ul>
        </section>

        <ActivityFeed className="lg:col-span-5" />
      </div>
    </>
  );
}
