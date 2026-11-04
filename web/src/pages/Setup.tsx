// First-time setup wizard: walks through everything the bot needs that
// isn't in the server's .env (roles, channels, API keys, realm, features).
// Every field saves immediately, so leaving halfway loses nothing.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Hash,
  KeyRound,
  PartyPopper,
  Shield,
  Sparkles,
  ToggleRight,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { api, type Channel, type Role, type Setting, type SetupState } from "../lib/api";
import { useLive } from "../lib/live";
import { useMe } from "../lib/me";
import { SettingRow } from "../components/SettingRow";
import { Integrations } from "../components/Integrations";
import { canRestart, useRestart } from "../components/Restart";
import { Badge, Button, Skeleton, cx } from "../components/ui";

interface Step {
  id: string;
  title: string;
  icon: LucideIcon;
  intro: string;
  keys?: string[];
}

const STEPS: Step[] = [
  { id: "welcome", title: "Welcome", icon: Sparkles, intro: "" },
  {
    id: "roles",
    title: "Roles",
    icon: Shield,
    intro:
      "Which roles the bot works with. Moderators can use the mod commands and this dashboard; approvers decide on applications.",
    keys: ["MOD_ROLE_ID", "APPROVER_ROLE_ID", "FRESH_ROLE_ID", "REGULAR_ROLE_ID", "ORGANIZER_ROLE_ID"],
  },
  {
    id: "channels",
    title: "Channels",
    icon: Hash,
    intro: "Where the bot posts. Optional channels can stay empty; that feature then simply stays off.",
    keys: [
      "REVIEW_CHANNEL_ID",
      "GEAR_CHECK_CHANNEL_ID",
      "ARCHIVE_CHANNEL_ID",
      "ANNOUNCEMENT_SANDBOX_CHANNEL_ID",
      "ATTENDANCE_CHANNEL_ID",
      "MODERATOR_CHANNEL_ID",
      "RAID_SUMMARY_FORUM_CHANNEL_ID",
      "RAID_LOGS_CHANNEL_ID",
      "RAID_LOGS_REPOST_CHANNEL_ID",
    ],
  },
  {
    id: "warcraftlogs",
    title: "WarcraftLogs",
    icon: KeyRound,
    intro:
      "API keys and your realm, used for gear checks, raid summaries and attendance. Keys are stored encrypted and never shown again after saving.",
    keys: ["SERVER_REGION", "SERVER_SLUG", "GUILD_NAME"],
  },
  {
    id: "features",
    title: "Features",
    icon: ToggleRight,
    intro: "Turn off anything your guild doesn't use. You can change this any time in Settings.",
  },
  { id: "finish", title: "Finish", icon: PartyPopper, intro: "" },
];

const REQUIRED_STEP: Record<string, string> = {
  MOD_ROLE_ID: "roles",
  FRESH_ROLE_ID: "roles",
  REVIEW_CHANNEL_ID: "channels",
  ANNOUNCEMENT_SANDBOX_CHANNEL_ID: "channels",
  ATTENDANCE_CHANNEL_ID: "channels",
  RAID_SUMMARY_FORUM_CHANNEL_ID: "channels",
  SERVER_SLUG: "warcraftlogs",
  WCL_CLIENT_ID: "warcraftlogs",
};

export default function SetupPage() {
  const navigate = useNavigate();
  const me = useMe();
  const { status, refreshStatus } = useLive();
  const { restart, overlay } = useRestart();
  const [stepIndex, setStepIndex] = useState(0);
  const [state, setState] = useState<SetupState | null>(null);
  const [settings, setSettings] = useState<Record<string, Setting>>({});
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [finishing, setFinishing] = useState(false);

  const loadState = () => api.get<SetupState>("/api/setup").then(setState).catch(() => undefined);

  useEffect(() => {
    loadState();
    api
      .get<{ settings: Setting[] }>("/api/settings")
      .then((d) => setSettings(Object.fromEntries(d.settings.map((s) => [s.key, s]))))
      .catch(() => undefined);
    api.get<Role[]>("/api/guild/roles").then(setRoles).catch(() => undefined);
    api.get<Channel[]>("/api/guild/channels").then(setChannels).catch(() => undefined);
  }, []);

  useEffect(() => {
    loadState();
    window.scrollTo({ top: 0 });
  }, [stepIndex]);

  const step = STEPS[stepIndex];
  const missingByStep = useMemo(() => {
    const result: Record<string, number> = {};
    state?.missing.forEach((m) => {
      const id = REQUIRED_STEP[m.key];
      if (id) result[id] = (result[id] ?? 0) + 1;
    });
    return result;
  }, [state]);

  const onSaved = (s: Setting) => {
    setSettings((prev) => ({ ...prev, [s.key]: s }));
    loadState();
    refreshStatus();
  };

  const row = (key: string) => {
    const setting = settings[key];
    if (!setting) return null;
    const required = REQUIRED_STEP[key] !== undefined;
    return (
      <div key={key}>
        <SettingRow
          badge={required ? <Badge tone="gold">Required</Badge> : <Badge>Optional</Badge>}
          setting={setting}
          roles={roles}
          channels={channels}
          onSaved={onSaved}
          compact
          channelFilter={key === "RAID_SUMMARY_FORUM_CHANNEL_ID" ? (c) => c.type === "forum" : undefined}
        />
      </div>
    );
  };

  const finish = async (andRestart: boolean) => {
    setFinishing(true);
    try {
      await api.post("/api/setup/complete");
      refreshStatus();
      if (andRestart) await restart();
      else navigate("/");
    } finally {
      setFinishing(false);
    }
  };

  const loading = !state || Object.keys(settings).length === 0;

  return (
    <>
      {overlay}
      <div className="mb-6">
        <div className="label mb-1.5 text-gold/90">● Setup</div>
        <h1 className="text-lg font-semibold tracking-tight">Set up the bot</h1>
        <p className="mt-1 text-muted">
          Everything is saved as you go. You can leave and come back; this page is always under System → Setup.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[220px_minmax(0,1fr)]">
        {/* stepper */}
        <ol className="scrollbar-thin -mx-4 flex gap-1.5 overflow-x-auto px-4 lg:mx-0 lg:flex-col lg:gap-1 lg:px-0">
          {STEPS.map((s, i) => {
            const missing = missingByStep[s.id] ?? 0;
            const done = i < stepIndex && !missing;
            return (
              <li key={s.id} className="shrink-0">
                <button
                  onClick={() => setStepIndex(i)}
                  className={cx(
                    "flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left transition lg:border-transparent",
                    i === stepIndex ? "border-gold/40 bg-gold/10 text-fg" : "border-line text-muted hover:bg-raised hover:text-fg",
                  )}
                >
                  <span
                    className={cx(
                      "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-2xs font-semibold",
                      done
                        ? "border-ok/40 bg-ok/15 text-ok"
                        : i === stepIndex
                          ? "border-gold/60 text-gold"
                          : "border-line text-faint",
                    )}
                  >
                    {done ? <Check className="h-3 w-3" /> : i + 1}
                  </span>
                  <span className="flex-1">{s.title}</span>
                  {missing > 0 && <span className="h-1.5 w-1.5 rounded-full bg-gold" title={`${missing} required`} />}
                </button>
              </li>
            );
          })}
        </ol>

        <div className="min-w-0">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-gold/30 bg-gold/10 text-gold">
              <step.icon className="h-4 w-4" />
            </div>
            <div>
              <div className="label">
                Step {stepIndex + 1} of {STEPS.length}
              </div>
              <h2 className="text-base font-semibold">{step.title}</h2>
            </div>
          </div>
          {step.intro && <p className="mb-5 max-w-3xl text-muted">{step.intro}</p>}

          {loading ? (
            <div className="space-y-3">
              <Skeleton className="h-28" />
              <Skeleton className="h-28" />
            </div>
          ) : step.id === "welcome" ? (
            <div className="space-y-4">
              <div className="card p-5">
                <div className="flex items-center gap-3">
                  {state.guild?.icon_url ? (
                    <img src={state.guild.icon_url} alt="" className="h-10 w-10 rounded-full" />
                  ) : (
                    <div className="h-10 w-10 rounded-full bg-raised" />
                  )}
                  <div>
                    <div className="font-semibold">Connected to {state.guild?.name ?? "your server"}</div>
                    <div className="text-muted">The bot is online. Next, tell it which roles and channels to use.</div>
                  </div>
                </div>
              </div>
              <div className="card p-5">
                <div className="label mb-3">Bot permissions in the server</div>
                <ul className="grid gap-2 sm:grid-cols-2">
                  {state.checks.map((c) => (
                    <li key={c.label} className="flex items-start gap-2">
                      {c.ok ? (
                        <CheckCircle2 className="mt-px h-4 w-4 shrink-0 text-ok" />
                      ) : (
                        <XCircle className="mt-px h-4 w-4 shrink-0 text-crimson" />
                      )}
                      <span className={c.ok ? "" : "text-crimson"}>{c.label}</span>
                    </li>
                  ))}
                </ul>
                {state.checks.some((c) => !c.ok) && (
                  <p className="mt-3 text-xs text-muted">
                    Fix these in Discord: Server Settings → Roles → the bot's role. Its role also has to be higher in the
                    list than any role it hands out.
                  </p>
                )}
              </div>
            </div>
          ) : step.id === "warcraftlogs" ? (
            <div className="space-y-3">
              <Integrations only={["warcraftlogs"]} />
              {step.keys!.map(row)}
              <div className="pt-2">
                <div className="label mb-2">Optional</div>
                <Integrations only={["blizzard"]} />
              </div>
            </div>
          ) : step.id === "features" ? (
            <div className="space-y-3">
              {Object.values(settings)
                .filter((s) => s.group === "features" && s.key !== "FEATURE_WEB_DASHBOARD_ENABLED")
                .map((s) => (
                  <SettingRow key={s.key} setting={s} roles={roles} channels={channels} onSaved={onSaved} compact />
                ))}
            </div>
          ) : step.id === "finish" ? (
            <div className="space-y-4">
              {state.missing.length > 0 ? (
                <div className="card border-warn/30 p-5">
                  <div className="font-semibold text-warn">Still missing</div>
                  <p className="mt-1 text-muted">
                    The bot runs without these, but the features that need them stay off until they're set.
                  </p>
                  <ul className="mt-3 space-y-1.5">
                    {state.missing.map((m) => (
                      <li key={m.key}>
                        <button
                          className="flex items-center gap-2 text-left hover:text-gold"
                          onClick={() => setStepIndex(STEPS.findIndex((s) => s.id === REQUIRED_STEP[m.key]))}
                        >
                          <span className="h-1.5 w-1.5 rounded-full bg-gold" /> {m.label}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="card border-ok/30 p-5">
                  <div className="flex items-center gap-2 font-semibold text-ok">
                    <CheckCircle2 className="h-4 w-4" /> Everything required is set
                  </div>
                </div>
              )}
              <div className="card p-5">
                <div className="font-semibold">Apply the settings</div>
                {canRestart(me, status) ? (
                  <p className="mt-1 text-muted">
                    Roles, channels and features take effect when the bot restarts. API keys are already active.
                  </p>
                ) : (
                  <p className="mt-1 text-muted">
                    Roles, channels and features take effect when the bot restarts. Restart it on the server (for
                    example <span className="font-mono text-fg">docker compose restart</span>). API keys are already
                    active.
                  </p>
                )}
                <div className="mt-4 flex flex-wrap gap-2">
                  {canRestart(me, status) && (
                    <Button variant="primary" loading={finishing} onClick={() => finish(true)}>
                      Finish and restart the bot
                    </Button>
                  )}
                  <Button variant={canRestart(me, status) ? "outline" : "primary"} loading={finishing} onClick={() => finish(false)}>
                    Finish
                  </Button>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-4">{step.keys!.map(row)}</div>
          )}

          <div className="mt-6 flex items-center justify-between border-t border-line pt-4">
            <Button
              variant="ghost"
              icon={<ArrowLeft className="h-4 w-4" />}
              disabled={stepIndex === 0}
              onClick={() => setStepIndex((i) => i - 1)}
            >
              Back
            </Button>
            <div className="flex gap-2">
              {stepIndex === 0 && (
                <Button variant="ghost" onClick={() => navigate("/")}>
                  Skip for now
                </Button>
              )}
              {stepIndex < STEPS.length - 1 && (
                <Button variant="primary" onClick={() => setStepIndex((i) => i + 1)}>
                  {stepIndex === 0 ? "Start" : "Next"} <ArrowRight className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
