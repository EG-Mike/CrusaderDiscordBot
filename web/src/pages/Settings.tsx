import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { ChevronDown, RotateCcw, Search, SlidersHorizontal } from "lucide-react";
import { api, type Channel, type Role, type Setting, type SettingGroup } from "../lib/api";
import { useLive } from "../lib/live";
import { SettingRow } from "../components/SettingRow";
import { Integrations } from "../components/Integrations";
import { NotificationsPanel } from "../components/NotificationsPanel";
import { RestartButton } from "../components/Restart";
import { Button, EmptyState, SectionHeader, Skeleton, cx } from "../components/ui";

interface SettingsResponse {
  groups: SettingGroup[];
  settings: Setting[];
}

type Filter = "all" | "changed" | "pending";

// --- page ------------------------------------------------------------------------

export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const { subscribe, refreshStatus } = useLive();
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const query = params.get("q") ?? "";
  const filter = (params.get("filter") as Filter) ?? "all";
  const group = params.get("group") ?? (query || filter !== "all" ? "" : "server");

  const setParam = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    Object.entries(updates).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    setParams(next, { replace: true });
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

  // Links like /settings?group=server#REVIEW_CHANNEL_ID: scroll to that
  // setting once it has rendered, and highlight it briefly.
  const { hash } = useLocation();
  useEffect(() => {
    if (!data || !hash) return;
    const el = document.getElementById(hash.slice(1));
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("ring-2", "ring-gold/50");
    const t = window.setTimeout(() => el.classList.remove("ring-2", "ring-gold/50"), 1800);
    return () => window.clearTimeout(t);
  }, [data, hash]);

  const visible = useMemo(() => {
    if (!data) return [];
    const q = query.toLowerCase();
    return data.settings.filter(
      (s) =>
        (!group || s.group === group) &&
        (filter === "all" || (filter === "changed" ? s.overridden : s.pending_restart)) &&
        (!q || s.key.toLowerCase().includes(q) || s.label.toLowerCase().includes(q) || s.help.toLowerCase().includes(q)),
    );
  }, [data, group, filter, query]);

  const onSaved = (updated: Setting) => {
    setData((d) => d && { ...d, settings: d.settings.map((s) => (s.key === updated.key ? updated : s)) });
    refreshStatus();
  };

  if (loadError) return <EmptyState title="Couldn't load settings">{loadError}</EmptyState>;

  const pendingCount = data?.settings.filter((s) => s.pending_restart).length ?? 0;
  const changedCount = data?.settings.filter((s) => s.overridden).length ?? 0;
  const activeGroup = data?.groups.find((g) => g.id === group);

  return (
    <>
      <SectionHeader
        eyebrow="Configuration"
        title="Settings"
        description="Everything from config/deployment.py. Changes are saved straight away; most take effect the next time the bot restarts, the ones marked Applies immediately are used at once."
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
        {/* group navigation: vertical on desktop, scrollable chips on mobile */}
        <nav className="scrollbar-thin -mx-4 flex gap-1.5 overflow-x-auto px-4 lg:mx-0 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:px-0">
          <div className="label hidden px-2.5 pb-1.5 lg:block">Sections</div>
          {data
            ? [
                ...data.groups.slice(0, 1),
                { id: "integrations", label: "Integrations", description: "", count: 2 },
                ...data.groups.slice(1),
              ].map((g) => (
                <button
                  key={g.id}
                  onClick={() => setParam({ group: g.id, filter: null, q: null })}
                  className={cx(
                    "flex shrink-0 items-center justify-between gap-3 rounded-lg border px-2.5 py-1.5 text-left text-[13px] transition lg:border-transparent",
                    group === g.id ? "border-gold/40 bg-gold/10 text-fg lg:border-transparent" : "border-line text-muted hover:bg-raised hover:text-fg",
                  )}
                >
                  {g.label}
                  <span className="text-2xs text-faint">{g.count}</span>
                </button>
              ))
            : [0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-8 w-28 lg:w-full" />)}
        </nav>

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
                  onClick={() => setParam({ filter: id === "all" ? null : id, group: id === "all" ? group || "server" : null })}
                  className={cx(
                    "rounded-md px-3 py-1.5 text-xs font-medium transition",
                    filter === id ? "bg-gold/15 text-gold" : "text-muted hover:text-fg",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {group === "integrations" && !query && filter === "all" && (
            <div className="mb-4">
              <h2 className="text-base font-semibold">Integrations</h2>
              <p className="text-muted">
                API keys for external services. Stored encrypted, never shown again after saving, and used straight
                away (no restart needed).
              </p>
            </div>
          )}

          {activeGroup && !query && filter === "all" && (
            <div className="mb-4">
              <h2 className="text-base font-semibold">{activeGroup.label}</h2>
              <p className="text-muted">{activeGroup.description}</p>
            </div>
          )}

          {group === "notifications" && !query && filter === "all" && <NotificationsPanel />}

          {group === "integrations" && !query && filter === "all" ? (
            <Integrations />
          ) : !data ? (
            <div className="space-y-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-32" />
              ))}
            </div>
          ) : visible.length === 0 ? (
            <div className="card">
              <EmptyState icon={<SlidersHorizontal className="h-6 w-6" />} title="Nothing here">
                {query ? `No setting matches "${query}".` : filter === "pending" ? "No changes are waiting for a restart." : "No settings have been changed from their defaults."}
              </EmptyState>
            </div>
          ) : (
            <div className="space-y-3">
              {visible.map((s) => (
                <SettingRow key={s.key} setting={s} roles={roles} channels={channels} onSaved={onSaved} />
              ))}
            </div>
          )}
          {data && visible.length > 0 && (
            <p className="mt-4 flex items-center gap-1.5 text-2xs text-faint">
              <ChevronDown className="h-3 w-3" /> {visible.length} setting{visible.length === 1 ? "" : "s"}
            </p>
          )}
        </div>
      </div>
    </>
  );
}
