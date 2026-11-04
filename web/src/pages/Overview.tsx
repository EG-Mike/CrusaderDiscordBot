import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  Clock,
  Power,
  RotateCcw,
  ScrollText,
  ToggleRight,
} from "lucide-react";
import type { Me } from "../lib/api";
import { formatDuration, useLive } from "../lib/live";
import { Badge, Skeleton, StatTile, cx } from "../components/ui";
import { RestartButton } from "../components/Restart";

const FEATURE_LABELS: Record<string, string> = {
  FEATURE_ANNOUNCEMENTS_ENABLED: "Announcements",
  FEATURE_EMOJI_ADMIN_ENABLED: "Emoji admin",
  FEATURE_ATTENDANCE_ENABLED: "Attendance",
  FEATURE_RAID_SUMMARY_ENABLED: "Raid summaries",
  FEATURE_RAID_LOGS_ENABLED: "Raid logs automation",
  FEATURE_TIER_RETROSPECTIVE_ENABLED: "Tier recap",
  FEATURE_POLLS_ENABLED: "Polls",
  FEATURE_AUDIT_LOG_ENABLED: "Audit log",
  FEATURE_WEB_DASHBOARD_ENABLED: "Web dashboard",
};

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
  const attention = status.warnings.length + (status.pending_restart.length ? 1 : 0) + (missing.length ? 1 : 0);

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
              <li key={m.key}>
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
                <span className={on ? "" : "text-faint"}>{FEATURE_LABELS[key] ?? key}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className="card lg:col-span-5">
          <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
            <div className="label">Recent activity</div>
            <Badge>Phase 1</Badge>
          </header>
          <div className="flex items-center gap-3 px-5 py-6 text-muted">
            <ScrollText className="h-4 w-4 text-faint" />
            The live audit feed arrives with the Audit Log page. Until then, use <span className="font-mono text-fg">/auditlog</span> in Discord.
          </div>
        </section>
      </div>
    </>
  );
}
