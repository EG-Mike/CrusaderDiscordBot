import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ChevronDown, History, Lock, RotateCcw, Search, SlidersHorizontal } from "lucide-react";
import {
  api,
  type Channel,
  type HistoryEntry,
  type Json,
  type Role,
  type Setting,
  type SettingGroup,
} from "../lib/api";
import { useLive } from "../lib/live";
import { useToast } from "../components/Toast";
import { Badge, Button, EmptyState, SectionHeader, Skeleton, Toggle, cx } from "../components/ui";

interface SettingsResponse {
  groups: SettingGroup[];
  settings: Setting[];
}

type Filter = "all" | "changed" | "pending";

const same = (a: Json, b: Json) => JSON.stringify(a) === JSON.stringify(b);

function formatValue(value: Json): string {
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (value === null || value === "") return "(empty)";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function utc(ts: string) {
  return new Date(ts.replace(" ", "T") + "Z").toLocaleString();
}

// --- pickers ------------------------------------------------------------------

function RolePicker({ value, roles, onChange, disabled }: { value: number | string; roles: Role[]; onChange: (v: string) => void; disabled?: boolean }) {
  const known = roles.some((r) => r.id === String(value));
  return (
    <select className="input" value={String(value)} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      <option value="0">None</option>
      {!known && String(value) !== "0" && <option value={String(value)}>Unknown role ({value})</option>}
      {roles.map((role) => (
        <option key={role.id} value={role.id}>
          @{role.name}
          {role.managed ? " (bot/integration)" : ""}
        </option>
      ))}
    </select>
  );
}

function ChannelPicker({ value, channels, onChange, disabled }: { value: number | string; channels: Channel[]; onChange: (v: string) => void; disabled?: boolean }) {
  const known = channels.some((c) => c.id === String(value));
  const byCategory = useMemo(() => {
    const groups = new Map<string, Channel[]>();
    channels.forEach((c) => groups.set(c.category ?? "No category", [...(groups.get(c.category ?? "No category") ?? []), c]));
    return [...groups.entries()];
  }, [channels]);
  const selected = channels.find((c) => c.id === String(value));
  return (
    <div>
      <select className="input" value={String(value)} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        <option value="0">None</option>
        {!known && String(value) !== "0" && <option value={String(value)}>Unknown channel ({value})</option>}
        {byCategory.map(([category, list]) => (
          <optgroup key={category} label={category}>
            {list.map((c) => (
              <option key={c.id} value={c.id}>
                #{c.name}
                {c.type === "forum" ? " (forum)" : ""}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      {selected && !selected.bot_can_view && (
        <p className="mt-1.5 text-xs text-warn">The bot can't see this channel. Check its permissions in Discord.</p>
      )}
    </div>
  );
}

// --- one setting ---------------------------------------------------------------

function SettingRow({
  setting,
  roles,
  channels,
  onSaved,
}: {
  setting: Setting;
  roles: Role[];
  channels: Channel[];
  onSaved: (s: Setting) => void;
}) {
  const toast = useToast();
  const isJson = setting.kind === "json";
  const initialDraft = () => (isJson ? JSON.stringify(setting.value, null, 2) : setting.value);
  const [draft, setDraft] = useState<Json>(initialDraft);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    setDraft(initialDraft());
    setError(null);
  }, [JSON.stringify(setting.value)]); // eslint-disable-line react-hooks/exhaustive-deps

  const parsedDraft = (): Json => {
    if (!isJson) return draft;
    return JSON.parse(draft as string);
  };

  let dirty = false;
  try {
    dirty = !same(parsedDraft(), setting.value);
  } catch {
    dirty = true;
  }

  const save = async (value?: Json) => {
    let body: Json;
    try {
      body = value !== undefined ? value : parsedDraft();
    } catch {
      setError("That isn't valid JSON. Check for a missing comma, quote or bracket.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await api.put<Setting>(`/api/settings/${setting.key}`, { value: body });
      onSaved(updated);
      setHistory(null);
      toast("ok", `${setting.label} saved${updated.pending_restart ? ". Restart the bot to apply it." : "."}`);
    } catch (e) {
      setError((e as Error).message);
      setDraft(initialDraft());
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    try {
      const updated = await api.del<Setting>(`/api/settings/${setting.key}`);
      onSaved(updated);
      setHistory(null);
      toast("ok", `${setting.label} reset to default.`);
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const toggleHistory = async () => {
    setShowHistory((v) => !v);
    if (!history) {
      try {
        setHistory(await api.get<HistoryEntry[]>(`/api/settings/${setting.key}/history`));
      } catch (e) {
        toast("error", (e as Error).message);
      }
    }
  };

  const ro = setting.read_only;
  const instant = setting.kind === "bool" || setting.kind === "enum" || setting.kind === "role" || setting.kind === "channel";
  const longHelp = setting.help.length > 260 || setting.help.split("\n").length > 3;

  let control;
  switch (setting.kind) {
    case "bool":
      control = <Toggle checked={setting.value as boolean} disabled={ro || saving} onChange={(v) => save(v)} label={setting.label} />;
      break;
    case "enum":
      control = (
        <select className="input max-w-xs" value={setting.value as string} disabled={ro || saving} onChange={(e) => save(e.target.value)}>
          {setting.options!.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      );
      break;
    case "role":
      control = <RolePicker value={setting.value as number | string} roles={roles} disabled={ro || saving} onChange={(v) => save(v)} />;
      break;
    case "channel":
      control = <ChannelPicker value={setting.value as number | string} channels={channels} disabled={ro || saving} onChange={(v) => save(v)} />;
      break;
    case "int":
    case "float":
      control = (
        <input
          className="input max-w-xs font-mono"
          inputMode="numeric"
          value={String(draft ?? "")}
          disabled={ro}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && dirty && save()}
        />
      );
      break;
    case "text":
      control = <textarea className="input min-h-24" value={draft as string} disabled={ro} onChange={(e) => setDraft(e.target.value)} />;
      break;
    case "json":
      control = (
        <textarea
          className="input min-h-32 font-mono text-xs leading-relaxed"
          spellCheck={false}
          value={draft as string}
          disabled={ro}
          onChange={(e) => setDraft(e.target.value)}
          rows={Math.min(16, (draft as string).split("\n").length + 1)}
        />
      );
      break;
    default:
      control = (
        <input
          className="input"
          value={draft as string}
          disabled={ro}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && dirty && save()}
        />
      );
  }

  const numericDirty = setting.kind === "int" || setting.kind === "float" ? String(draft) !== String(setting.value) : dirty;

  return (
    <div id={setting.key} className="card scroll-mt-24 p-4 md:p-5">
      <div className={cx("flex gap-4", setting.kind === "bool" ? "items-start" : "flex-col")}>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{setting.label}</h3>
            {setting.overridden && <Badge tone="gold">Changed</Badge>}
            {setting.pending_restart && <Badge tone="warn">Pending restart</Badge>}
            {setting.default_source === "local" && <Badge tone="info">Local file</Badge>}
            {ro && (
              <Badge>
                <Lock className="h-2.5 w-2.5" /> Read-only
              </Badge>
            )}
          </div>
          <div className="mt-0.5 font-mono text-2xs text-faint">{setting.key}</div>
          {setting.help && (
            <div className="mt-2 max-w-3xl text-muted">
              <div className={cx("whitespace-pre-line", !showHelp && longHelp && "line-clamp-3")}>{setting.help}</div>
              {longHelp && (
                <button className="mt-1 text-xs text-gold hover:underline" onClick={() => setShowHelp((v) => !v)}>
                  {showHelp ? "Show less" : "More"}
                </button>
              )}
            </div>
          )}
          {ro && setting.read_only_reason && <p className="mt-2 text-xs text-faint">{setting.read_only_reason}</p>}
        </div>
        <div className={setting.kind === "bool" ? "pt-0.5" : "w-full"}>{control}</div>
      </div>

      {error && <p className="mt-2 text-xs text-crimson">{error}</p>}

      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3 text-xs text-faint">
        <span className="mr-auto min-w-0 truncate">
          Default: <span className="font-mono text-muted">{formatValue(setting.default).slice(0, 120)}</span>
          {setting.updated_at && (
            <>
              {" "}· changed {utc(setting.updated_at)}
              {setting.updated_by ? ` by ${setting.updated_by.replace(/ \(\d+\)$/, "")}` : ""}
            </>
          )}
        </span>
        {!instant && numericDirty && !ro && (
          <>
            <Button size="sm" variant="ghost" onClick={() => setDraft(initialDraft())}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" loading={saving} onClick={() => save()}>
              Save
            </Button>
          </>
        )}
        <Button size="sm" variant="ghost" icon={<History className="h-3.5 w-3.5" />} onClick={toggleHistory}>
          History
        </Button>
        {setting.overridden && !ro && (
          <Button size="sm" variant="ghost" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={reset} disabled={saving}>
            Reset
          </Button>
        )}
      </div>

      {showHistory && (
        <div className="mt-3 rounded-lg border border-line bg-bg/60">
          {history === null ? (
            <Skeleton className="m-3 h-10" />
          ) : history.length === 0 ? (
            <p className="px-3 py-3 text-xs text-faint">No changes made from the dashboard yet.</p>
          ) : (
            <ul className="divide-y divide-line text-xs">
              {history.map((h) => (
                <li key={h.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                  <span className="text-faint">{utc(h.changed_at)}</span>
                  <span className="text-muted">{h.changed_by?.replace(/ \(\d+\)$/, "") ?? "unknown"}</span>
                  <span className="min-w-0 flex-1 truncate font-mono">
                    <span className="text-faint line-through">{h.old_value === null ? "default" : formatValue(h.old_value)}</span>
                    {"  →  "}
                    <span className="text-fg">{h.new_value === null ? "default" : formatValue(h.new_value)}</span>
                  </span>
                  {!ro && (
                    <button
                      className="text-gold hover:underline"
                      onClick={() => (h.old_value === null ? reset() : save(h.old_value))}
                      title="Set the value back to what it was before this change"
                    >
                      Revert
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

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
  const group = params.get("group") ?? (query || filter !== "all" ? "" : "general");

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
        description="Everything from config/deployment.py. Changes are saved straight away and take effect the next time the bot restarts."
      />

      {pendingCount > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <RotateCcw className="h-4 w-4 text-warn" />
          <span className="flex-1">
            <span className="font-medium text-warn">
              {pendingCount} change{pendingCount === 1 ? "" : "s"} waiting for a restart.
            </span>{" "}
            <span className="text-muted">Restart the bot in AMP to apply {pendingCount === 1 ? "it" : "them"}.</span>
          </span>
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
            ? data.groups.map((g) => (
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
                  onClick={() => setParam({ filter: id === "all" ? null : id, group: id === "all" ? group || "general" : null })}
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

          {activeGroup && !query && filter === "all" && (
            <div className="mb-4">
              <h2 className="text-base font-semibold">{activeGroup.label}</h2>
              <p className="text-muted">{activeGroup.description}</p>
            </div>
          )}

          {!data ? (
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
