import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { CheckCircle2, ChevronDown, Rocket, RotateCcw, Search, SlidersHorizontal } from "lucide-react";
import { api, type Channel, type Role, type Setting, type SettingGroup, type SettingSection, type SettingTab } from "../lib/api";
import { useLive } from "../lib/live";
import { SettingRow } from "../components/SettingRow";
import { GuildPanel } from "../components/GuildPanel";
import { Integrations } from "../components/Integrations";
import { NotificationsPanel } from "../components/NotificationsPanel";
import { RestartButton } from "../components/Restart";
import { Button, EmptyState, SectionHeader, Skeleton, Toggle, cx } from "../components/ui";

interface SettingsResponse {
  sections: SettingSection[];
  tabs: SettingTab[];
  groups: SettingGroup[];
  settings: Setting[];
}

type Filter = "all" | "changed" | "pending";

// Groups with their own panel above (or instead of) their settings.
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
  // ?group= takes a tab or any group inside one (older links name the group).
  const tabFor = (id: string | undefined) => data?.tabs.find((t) => t.id === id || (id && t.groups.includes(id)))?.id;
  const targetGroup = data?.settings.find((s) => s.key === target)?.group;
  const param = params.get("group");
  const group = param ? tabFor(param) ?? param : browsing ? tabFor(targetGroup) ?? "guild" : "";
  const activeTab = data?.tabs.find((t) => t.id === group);
  const tabGroups = activeTab?.groups ?? [group];

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
    if (!data) return;
    // A link to a group inside a combined tab (e.g. ?group=api_keys) scrolls to that part.
    const subsection = !target && param && param !== group ? `section-${param}` : "";
    if (!target && !subsection) return;
    const el = document.getElementById(target || subsection);
    if (!el) return;
    if (subsection) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("ring-2", "ring-gold/50");
    const t = window.setTimeout(() => el.classList.remove("ring-2", "ring-gold/50"), 1800);
    return () => window.clearTimeout(t);
  }, [data, target, param, group]);

  // Advanced settings show when asked for, when searching, or when linked to directly.
  const shown = (s: Setting) => showAdvanced || !s.advanced || s.key === target || !browsing;

  const visible = useMemo(() => {
    if (!data) return [];
    const q = query.toLowerCase();
    return data.settings.filter(
      (s) =>
        (!group || tabGroups.includes(s.group)) &&
        shown(s) &&
        (filter === "all" || (filter === "changed" ? s.overridden : s.pending_restart)) &&
        (!q || s.key.toLowerCase().includes(q) || s.label.toLowerCase().includes(q) || s.help.toLowerCase().includes(q)),
    );
  }, [data, group, activeTab, filter, query, showAdvanced, target]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSaved = (updated: Setting) => {
    setData((d) => d && { ...d, settings: d.settings.map((s) => (s.key === updated.key ? updated : s)) });
    refreshStatus();
  };

  if (loadError) return <EmptyState title="Couldn't load settings">{loadError}</EmptyState>;

  const pendingCount = data?.settings.filter((s) => s.pending_restart).length ?? 0;
  const changedCount = data?.settings.filter((s) => s.overridden).length ?? 0;
  const shownCount = (t: SettingTab | SettingGroup) => t.count - (showAdvanced ? 0 : t.advanced_count);
  const tabs = (sectionId: string) =>
    (data?.tabs ?? []).filter((t) => t.section === sectionId && (shownCount(t) > 0 || t.groups.some((g) => PANELS.has(g))));
  const hiddenAdvanced = activeTab && browsing && !showAdvanced ? activeTab.advanced_count : 0;
  const groupInfo = (id: string) => data?.groups.find((g) => g.id === id);

  const panel = (id: string) =>
    id === "guild" ? <GuildPanel /> : id === "notifications" ? <NotificationsPanel /> : id === "setup" ? <SetupPanel /> : id === "api_keys" ? <Integrations /> : null;

  const rows = (list: Setting[]) => (
    <div className="space-y-3">
      {list.map((s) => (
        <SettingRow key={s.key} setting={s} roles={roles} channels={channels} onSaved={onSaved} />
      ))}
    </div>
  );

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
                  {tabs(section.id).map((t) => (
                    <button
                      key={t.id}
                      onClick={() => setParam({ group: t.id, filter: null, q: null })}
                      className={cx(
                        "flex w-full items-center justify-between gap-3 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition",
                        group === t.id ? "bg-gold/10 text-fg" : "text-muted hover:bg-raised hover:text-fg",
                      )}
                    >
                      {t.label}
                      {shownCount(t) > 0 && <span className="text-2xs text-faint">{shownCount(t)}</span>}
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
              {tabs(section.id).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
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

          {activeTab && browsing && (
            <div className="mb-4">
              <h2 className="text-base font-semibold">{activeTab.label}</h2>
              <p className="text-muted">{activeTab.description}</p>
            </div>
          )}

          {!data ? (
            <div className="space-y-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-32" />
              ))}
            </div>
          ) : browsing && activeTab ? (
            // One tab: its groups in order, each with its panel and settings
            // (with a heading when the tab combines several).
            <div className="space-y-8">
              {activeTab.groups.map((gid) => {
                const info = groupInfo(gid);
                const list = visible.filter((s) => s.group === gid);
                const own = panel(gid);
                if (!own && list.length === 0 && activeTab.groups.length > 1) return null;
                return (
                  <section key={gid} id={`section-${gid}`} className="scroll-mt-24">
                    {activeTab.groups.length > 1 && info && (
                      <div className="mb-3 border-b border-line pb-2">
                        <h3 className="font-semibold">{info.label}</h3>
                        <p className="text-muted">{info.description}</p>
                      </div>
                    )}
                    {own}
                    {list.length > 0 ? (
                      rows(list)
                    ) : !own ? (
                      <div className="card">
                        <EmptyState icon={<SlidersHorizontal className="h-6 w-6" />} title="Nothing here">
                          Only advanced settings here. Switch on Advanced to see them.
                        </EmptyState>
                      </div>
                    ) : null}
                  </section>
                );
              })}
            </div>
          ) : visible.length === 0 ? (
            <div className="card">
              <EmptyState icon={<SlidersHorizontal className="h-6 w-6" />} title="Nothing here">
                {query
                  ? `No setting matches "${query}".`
                  : filter === "pending"
                    ? "No changes are waiting for a restart."
                    : "No settings have been changed from their defaults."}
              </EmptyState>
            </div>
          ) : (
            rows(visible)
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
