import { useEffect, useMemo, useState, type ReactNode } from "react";
import { History, Lock, RotateCcw } from "lucide-react";
import { api, type Channel, type HistoryEntry, type Json, type Role, type Setting } from "../lib/api";
import { useToast } from "./Toast";
import { Badge, Button, Skeleton, Toggle, cx } from "./ui";

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

export function SettingRow({
  setting,
  roles,
  channels,
  onSaved,
  compact = false,
  channelFilter,
  badge,
}: {
  setting: Setting;
  roles: Role[];
  channels: Channel[];
  onSaved: (s: Setting) => void;
  /** Setup wizard: no history/reset footer, help text clamped. */
  compact?: boolean;
  /** Restrict the channel dropdown, e.g. to forum channels. */
  channelFilter?: (c: Channel) => boolean;
  /** Extra badge next to the label (e.g. Required/Optional). */
  badge?: ReactNode;
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
      // The setup wizard saves many fields in a row and restarts at the
      // end, so a toast per field would just be noise there.
      if (!compact)
        toast("ok", `${setting.label} saved${updated.live ? " and applied." : updated.pending_restart ? ". Restart the bot to apply it." : "."}`);
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
      control = (
        <ChannelPicker
          value={setting.value as number | string}
          channels={channelFilter ? channels.filter(channelFilter) : channels}
          disabled={ro || saving}
          onChange={(v) => save(v)}
        />
      );
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
    <div id={setting.key} className={cx("card scroll-mt-24", compact ? "p-4" : "p-4 md:p-5")}>
      <div className={cx("flex gap-4", setting.kind === "bool" ? "items-start" : "flex-col")}>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{setting.label}</h3>
            {badge}
            {setting.overridden && !compact && <Badge tone="gold">Changed</Badge>}
            {setting.pending_restart && !compact && <Badge tone="warn">Pending restart</Badge>}
            {setting.live && !compact && <Badge tone="ok">Applies immediately</Badge>}
            {setting.default_source === "local" && <Badge tone="info">Local file</Badge>}
            {setting.default_source === "env" && !setting.overridden && <Badge tone="info">From .env</Badge>}
            {ro && (
              <Badge>
                <Lock className="h-2.5 w-2.5" /> Read-only
              </Badge>
            )}
          </div>
          {!compact && <div className="mt-0.5 font-mono text-2xs text-faint">{setting.key}</div>}
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

      {compact ? (
        !instant && numericDirty && !ro && (
          <div className="mt-3 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setDraft(initialDraft())}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" loading={saving} onClick={() => save()}>
              Save
            </Button>
          </div>
        )
      ) : (
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3 text-xs text-faint">
        <span className="mr-auto min-w-0 truncate">
          Default:{" "}
          <span className="font-mono text-muted">
            {(setting.kind === "role" || setting.kind === "channel") && String(setting.default) === "0"
              ? "not set"
              : formatValue(setting.default).slice(0, 120)}
          </span>
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
      )}

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

