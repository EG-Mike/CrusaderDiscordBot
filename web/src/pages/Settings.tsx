import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { CheckCircle2, ChevronDown, Rocket, RotateCcw, Search, SlidersHorizontal } from "lucide-react";
import { api, type Channel, type Role, type Setting, type SettingGroup, type SettingSection } from "../lib/api";
import { useLive } from "../lib/live";
import { SettingRow } from "../components/SettingRow";
import { GuildPanel } from "../components/GuildPanel";
import { Integrations } from "../components/Integrations";
import { NotificationsPanel } from "../components/NotificationsPanel";
import { RestartButton } from "../components/Restart";
import { Button, EmptyState, SectionHeader, Skeleton, Toggle, cx } from "../components/ui";

interface SettingsResponse {
  sections: SettingSection[];
  groups: SettingGroup[];
  settings: Setting[];
}

type Filter = "all" | "changed" | "pending";

// Tabs with their own panel above (or instead of) the settings list.
const PANELS = new Set(["guild", "api_keys", "notifications", "setup"]);
const ADVANCED_KEY = "settings.showAdvanced";

function readAdvanced() {
  try {
    return localStorage.getItem(ADVANCED_KEY) === "1";
  } catch {
    return false;
  }
}

function SetupPanel() {
  const { status } = useLive();
  const missing = status?.missing_setup ?? [];
  return (
    <section className="card mb-3 p-4 md:p-5">
      <div className="flex flex-wrap items-start gap-3">
        {status?.setup_completed ? <CheckCircle2 className="mt-0.5 h-5 w-5 text-ok" /> : <Rocket className="mt-0.5 h-5 w-5 text-gold" />}
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold">{status?.setup_completed ? "Setup is done" : "Setup isn't finished yet"}</h3>
          <p className="mt-1 text-muted">
            The setup guide walks through the roles, channels, API keys and features the bot needs, one step at a time. Everything it sets
            can also be changed here in Settings.
          </p>
          {missing.length > 0 && (
            <p className="mt-2 text-warn">
              Still missing: {missing.map((m) => m.label).join(", ")}.
            </p>
          )}
        </div>
        <Link to="/setup" className="inline-flex h-7 shrink-0 items-center rounded-lg border border-line px-2.5 text-xs font-medium text-muted transition hover:text-fg">
          {status?.setup_completed ? "Run the setup guide again" : "Open the setup guide"}
        </Link>
      </div>
    </section>
  );
}

export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const { subscribe, refreshStatus } = useLive();
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(readAdvanced);
  const { hash } = useLocation();
  const target = hash.slice(1);

  const query = params.get("q") ?? "";
  const filter = (params.get("filter") as Filter) ?? "all";
  const browsing = !query && filter === "all";
  const targetGroup = data?.settings.find((s) => s.key === target)?.group;
  const group = params.get("group") ?? (browsing ? targetGroup ?? "guild" : "");

  const setParam = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    Object.entries(updates).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    setParams(next, { replace: true });
  };

  const toggleAdvanced = (on: boolean) => {
    setShowAdvanced(on);
    try {
      localStorage.setItem(ADVANCED_KEY, on ? "1" : "0");
    } catch {
      /* private window: just not remembered */
    }
  };

  const load = () =>
    api
      .get<SettingsResponse>("/api/settings")
      .then(setData)
      .catch((e) => setLoadError((e as Error).message));

  useEffect(() => {
    load();
    api.get<Role[]>("/api/guild/roles").then(setRoles).catch(() => undefined);
    api.get<Channel[]>("/api/guild/channels").then(setChannels).catch(() => undefined);
    // Another moderator saved something: refresh.
    return subscribe((event) => event.type === "settings.changed" && load());
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Links like /settings#REVIEW_CHANNEL_ID: scroll to that setting once it
  // has rendered, and highlight it briefly.
  useEffect(() => {
    if (!data || !target) return;
    const el = document.getElementById(target);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("ring-2", "ring-gold/50");
    const t = window.setTimeout(() => el.classList.remove("ring-2", "ring-gold/50"), 1800);
    return () => window.clearTimeout(t);
  }, [data, target]);

  // Advanced settings show when asked for, when searching, or when linked to directly.
  const shown = (s: Setting) => showAdvanced || !s.advanced || s.key === target || !browsing;

  const visible = useMemo(() => {
    if (!data) return [];
    const q = query.toLowerCase();
    return data.settings.filter(
      (s) =>
        (!group || s.group === group) &&
        shown(s) &&
        (filter === "all" || (filter === "changed" ? s.overridden : s.pending_restart)) &&
        (!q || s.key.toLowerCase().includes(q) || s.label.toLowerCase().includes(q) || s.help.toLowerCase().includes(q)),
    );
  }, [data, group, filter, query, showAdvanced, target]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSaved = (updated: Setting) => {
    setData((d) => d && { ...d, settings: d.settings.map((s) => (s.key === updated.key ? updated : s)) });
    refreshStatus();
  };

  if (loadError) return <EmptyState title="Couldn't load settings">{loadError}</EmptyState>;

  const pendingCount = data?.settings.filter((s) => s.pending_restart).length ?? 0;
  const changedCount = data?.settings.filter((s) => s.overridden).length ?? 0;
  const activeGroup = data?.groups.find((g) => g.id === group);
  const tabCount = (g: SettingGroup) => g.count - (showAdvanced ? 0 : g.advanced_count);
  const tabs = (sectionId: string) => (data?.groups ?? []).filter((g) => g.section === sectionId && (tabCount(g) > 0 || PANELS.has(g.id)));
  const hiddenAdvanced = activeGroup && browsing && !showAdvanced ? activeGroup.advanced_count : 0;

  return (
    <>
      <SectionHeader
        eyebrow="Configuration"
        title="Settings"
        description="Changes are saved straight away. Most apply after the next restart; the ones marked Applies immediately are used at once."
      />

      {pendingCount > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <RotateCcw className="h-4 w-4 text-warn" />
          <span className="flex-1">
            <span className="font-medium text-warn">
              {pendingCount} change{pendingCount === 1 ? "" : "s"} waiting for a restart.
            </span>{" "}
            <span className="text-muted">Restart the bot to apply {pendingCount === 1 ? "it" : "them"}.</span>
          </span>
          <RestartButton />
          <Button size="sm" variant="outline" onClick={() => setParam({ filter: "pending", group: null, q: null })}>
            Show
          </Button>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[220px_minmax(0,1fr)]">
        {/* navigation: a list per section on desktop, one dropdown on phones */}
        <nav className="hidden lg:block">
          {data
            ? data.sections.map((section) => (
                <div key={section.id} className="mb-4">
                  <div className="label px-2.5 pb-1.5">{section.label}</div>
                  {tabs(section.id).map((g) => (
                    <button
                      key={g.id}
                      onClick={() => setParam({ group: g.id, filter: null, q: null })}
                      className={cx(
                        "flex w-full items-center justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition",
                        group === g.id ? "bg-gold/10 text-fg" : "text-muted hover:bg-raised hover:text-fg",
                      )}
                    >
                      {g.label}
                      {tabCount(g) > 0 && <span className="text-2xs text-faint">{tabCount(g)}</span>}
                    </button>
                  ))}
                </div>
              ))
            : [0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="mb-1 h-8 w-full" />)}
        </nav>
        <select
          className="input lg:hidden"
          aria-label="Settings section"
          value={group}
          onChange={(e) => setParam({ group: e.target.value, filter: null, q: null })}
        >
          {!group && <option value="">Search results</option>}
          {data?.sections.map((section) => (
            <optgroup key={section.id} label={section.label}>
              {tabs(section.id).map((g) => (
                <option key={g.id} value={g.id}>
                  {g.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>

        <div className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <div className="relative min-w-[200px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
              <input
                className="input pl-9"
                placeholder="Search all settings"
                value={query}
                onChange={(e) => setParam({ q: e.target.value || null, group: null })}
              />
            </div>
            <div className="flex rounded-lg border border-line bg-surface p-0.5">
              {(
                [
                  ["all", "All"],
                  ["changed", `Changed${changedCount ? ` · ${changedCount}` : ""}`],
                  ["pending", `Pending${pendingCount ? ` · ${pendingCount}` : ""}`],
                ] as [Filter, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setParam({ filter: id === "all" ? null : id, group: id === "all" ? group || "guild" : null })}
                  className={cx(
                    "rounded-md px-3 py-1.5 text-xs font-medium transition",
                    filter === id ? "bg-gold/15 text-gold" : "text-muted hover:text-fg",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-xs text-muted">
              <Toggle checked={showAdvanced} onChange={toggleAdvanced} label="Show advanced settings" />
              Advanced
            </label>
          </div>

          {activeGroup && browsing && (
            <div className="mb-4">
              <h2 className="text-base font-semibold">{activeGroup.label}</h2>
              <p className="text-muted">{activeGroup.description}</p>
            </div>
          )}

          {browsing && group === "guild" && <GuildPanel />}
          {browsing && group === "notifications" && <NotificationsPanel />}
          {browsing && group === "setup" && <SetupPanel />}

          {browsing && group === "api_keys" ? (
            <Integrations />
          ) : !data ? (
            <div className="space-y-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-32" />
              ))}
            </div>
          ) : visible.length === 0 ? (
            browsing && PANELS.has(group) ? null : (
              <div className="card">
                <EmptyState icon={<SlidersHorizontal className="h-6 w-6" />} title="Nothing here">
                  {query
                    ? `No setting matches "${query}".`
                    : filter === "pending"
                      ? "No changes are waiting for a restart."
                      : filter === "changed"
                        ? "No settings have been changed from their defaults."
                        : "Only advanced settings here. Switch on Advanced to see them."}
                </EmptyState>
              </div>
            )
          ) : (
            <div className="space-y-3">
              {visible.map((s) => (
                <SettingRow key={s.key} setting={s} roles={roles} channels={channels} onSaved={onSaved} />
              ))}
            </div>
          )}
          {data && (visible.length > 0 || hiddenAdvanced > 0) && (
            <p className="mt-4 flex flex-wrap items-center gap-1.5 text-2xs text-faint">
              <ChevronDown className="h-3 w-3" /> {visible.length} setting{visible.length === 1 ? "" : "s"}
              {hiddenAdvanced > 0 && (
                <>
                  {" "}· {hiddenAdvanced} advanced hidden ·{" "}
                  <button className="text-gold hover:underline" onClick={() => toggleAdvanced(true)}>
                    Show
                  </button>
                </>
              )}
            </p>
          )}
        </div>
      </div>
    </>
  );
}
