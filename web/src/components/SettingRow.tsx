import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowUp, History, Plus, RotateCcw, X } from "lucide-react";
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

// --- list and role editors (save on every change) ---------------------------------

function Chip({ children, onRemove, disabled }: { children: ReactNode; onRemove: () => void; disabled?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-line bg-raised py-0.5 pl-2 pr-1 text-xs">
      {children}
      <button type="button" onClick={onRemove} disabled={disabled} className="rounded p-0.5 text-faint hover:text-crimson" aria-label="Remove">
        <X className="h-3 w-3" />
      </button>
    </span>
  );
}

function ListEditor({ value, numbers, onChange, disabled }: { value: Json[]; numbers: boolean; onChange: (v: Json[]) => void; disabled?: boolean }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = () => {
    const parts = text.split(",").map((t) => t.trim()).filter(Boolean);
    if (!parts.length) return;
    const parsed = numbers ? parts.map(Number) : parts;
    if (numbers && parsed.some((n) => !Number.isInteger(n) || (n as number) < 0)) {
      setError("Whole numbers only.");
      return;
    }
    const next = [...value, ...parsed.filter((p) => !value.includes(p))];
    if (numbers) next.sort((a, b) => (a as number) - (b as number));
    setText("");
    setError(null);
    onChange(next);
  };
  return (
    <div>
      <div className="flex flex-wrap gap-1.5">
        {value.map((item, i) => (
          <Chip key={`${item}-${i}`} disabled={disabled} onRemove={() => onChange(value.filter((_, j) => j !== i))}>
            {String(item)}
          </Chip>
        ))}
        {value.length === 0 && <span className="text-xs text-faint">None</span>}
      </div>
      <div className="mt-2 flex max-w-md gap-2">
        <input
          className="input h-8 text-xs"
          value={text}
          disabled={disabled}
          inputMode={numbers ? "numeric" : undefined}
          placeholder={numbers ? "Add a number (or several, comma-separated)" : "Add (or several, comma-separated)"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <Button size="sm" icon={<Plus className="h-3.5 w-3.5" />} onClick={add} disabled={disabled || !text.trim()}>
          Add
        </Button>
      </div>
      {error && <p className="mt-1 text-xs text-crimson">{error}</p>}
    </div>
  );
}

/** Fixed names (e.g. Tank, Mage), each with a role picker. */
function RoleMapEditor({ value, roles, onChange, disabled }: { value: Record<string, Json>; roles: Role[]; onChange: (v: Record<string, Json>) => void; disabled?: boolean }) {
  return (
    <div className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
      {Object.entries(value).map(([name, roleId]) => (
        <label key={name} className="grid grid-cols-[80px_minmax(0,1fr)] items-center gap-2">
          <span className="truncate text-xs text-muted">{name}</span>
          <RolePicker value={roleId as string} roles={roles} disabled={disabled} onChange={(id) => onChange({ ...value, [name]: id })} />
        </label>
      ))}
    </div>
  );
}

/** A set of roles, stored as {role ID: role name}. */
function RoleSetEditor({ value, roles, onChange, disabled }: { value: Record<string, Json>; roles: Role[]; onChange: (v: Record<string, Json>) => void; disabled?: boolean }) {
  const name = (id: string) => roles.find((r) => r.id === id)?.name ?? String(value[id] ?? `Unknown role (${id})`);
  const available = roles.filter((r) => !(r.id in value) && !r.managed);
  return (
    <div>
      <div className="flex flex-wrap gap-1.5">
        {Object.keys(value).map((id) => (
          <Chip key={id} disabled={disabled} onRemove={() => onChange(Object.fromEntries(Object.entries(value).filter(([k]) => k !== id)))}>
            @{name(id)}
          </Chip>
        ))}
        {Object.keys(value).length === 0 && <span className="text-xs text-faint">None</span>}
      </div>
      <select
        className="input mt-2 h-8 max-w-xs text-xs"
        value=""
        disabled={disabled}
        onChange={(e) => {
          const role = roles.find((r) => r.id === e.target.value);
          if (role) onChange({ ...value, [role.id]: role.name });
        }}
      >
        <option value="">Add a role…</option>
        {available.map((r) => (
          <option key={r.id} value={r.id}>
            @{r.name}
          </option>
        ))}
      </select>
    </div>
  );
}

/** An ordered pick from fixed options (e.g. which Blizzard data to search, in order). */
function ChoiceListEditor({
  value,
  options,
  onChange,
  disabled,
  allowEmpty,
}: {
  value: Json[];
  options: { value: string | number; label: string }[];
  onChange: (v: Json[]) => void;
  disabled?: boolean;
  /** The list may be empty (its default is), e.g. "any ruleset". */
  allowEmpty?: boolean;
}) {
  const label = (v: Json) => options.find((o) => o.value === v)?.label ?? String(v);
  const remaining = options.filter((o) => !value.includes(o.value));
  const moveUp = (i: number) => onChange([...value.slice(0, i - 1), value[i], value[i - 1], ...value.slice(i + 1)]);
  return (
    <div>
      <ol className="flex flex-wrap gap-1.5">
        {value.map((v, i) => (
          <li key={String(v)} className="inline-flex items-center gap-1 rounded-md border border-line bg-raised py-0.5 pl-1.5 pr-1 text-xs">
            <span className="tabular-nums text-faint">{i + 1}.</span>
            {label(v)}
            {i > 0 && (
              <button type="button" onClick={() => moveUp(i)} disabled={disabled} className="rounded p-0.5 text-faint hover:text-fg" aria-label={`Search ${label(v)} earlier`} title="Search this earlier">
                <ArrowUp className="h-3 w-3" />
              </button>
            )}
            {(value.length > 1 || allowEmpty) && (
              <button type="button" onClick={() => onChange(value.filter((x) => x !== v))} disabled={disabled} className="rounded p-0.5 text-faint hover:text-crimson" aria-label={`Remove ${label(v)}`}>
                <X className="h-3 w-3" />
              </button>
            )}
          </li>
        ))}
        {value.length === 0 && <li className="text-xs text-faint">None (any)</li>}
      </ol>
      {remaining.length > 0 && (
        <select
          className="input mt-2 h-8 max-w-xs text-xs"
          value=""
          disabled={disabled}
          onChange={(e) => {
            const option = remaining.find((o) => String(o.value) === e.target.value);
            if (option) onChange([...value, option.value]);
          }}
        >
          <option value="">Add…</option>
          {remaining.map((o) => (
            <option key={String(o.value)} value={String(o.value)}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function describeDefault(setting: Setting): string {
  const d = setting.default;
  if ((setting.kind === "role" || setting.kind === "channel") && String(d) === "0") return "not set";
  if (setting.widget === "choice_list")
    return (d as Json[]).map((v) => setting.options?.find((o) => o.value === v)?.label ?? String(v)).join(", then ") || "none (any)";
  if (setting.options) return setting.options.find((o) => o.value === d)?.label ?? formatValue(d);
  if (setting.widget === "text_list" || setting.widget === "number_list") return (d as Json[]).length ? (d as Json[]).join(", ") : "none";
  if (setting.widget === "role_map" || setting.widget === "role_set") return `${Object.keys(d as object).length} roles`;
  if (setting.kind === "string" && d === "") return "empty";
  return formatValue(d) + (setting.unit && (setting.kind === "int" || setting.kind === "float") ? ` ${setting.unit}` : "");
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
  const isJson = setting.kind === "json" && !setting.widget;
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

  const instant = setting.kind === "bool" || setting.kind === "enum" || setting.kind === "role" || setting.kind === "channel" || setting.widget !== null;
  const longHelp = setting.help.length > 260 || setting.help.split("\n").length > 3;

  let control;
  switch (setting.kind) {
    case "bool":
      control = <Toggle checked={setting.value as boolean} disabled={saving} onChange={(v) => save(v)} label={setting.label} />;
      break;
    case "enum":
      control = (
        <select
          className="input max-w-xs"
          value={String(setting.value)}
          disabled={saving}
          onChange={(e) => save(setting.options!.find((o) => String(o.value) === e.target.value)!.value)}
        >
          {setting.options!.map((o) => (
            <option key={String(o.value)} value={String(o.value)}>
              {o.label}
            </option>
          ))}
        </select>
      );
      break;
    case "role":
      control = <RolePicker value={setting.value as number | string} roles={roles} disabled={saving} onChange={(v) => save(v)} />;
      break;
    case "channel":
      control = (
        <ChannelPicker
          value={setting.value as number | string}
          channels={channelFilter ? channels.filter(channelFilter) : channels}
          disabled={saving}
          onChange={(v) => save(v)}
        />
      );
      break;
    case "int":
    case "float":
      control = (
        <div className="flex items-center gap-2">
          <input
            className={cx("input font-mono", setting.unit ? "max-w-[9rem]" : "max-w-xs")}
            inputMode="numeric"
            value={String(draft ?? "")}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && dirty && save()}
          />
          {setting.unit && <span className="text-xs text-muted">{setting.unit}</span>}
        </div>
      );
      break;
    case "text":
      control = <textarea className="input min-h-24" value={draft as string} onChange={(e) => setDraft(e.target.value)} />;
      break;
    case "json":
      if (setting.widget === "text_list" || setting.widget === "number_list") {
        control = <ListEditor value={setting.value as Json[]} numbers={setting.widget === "number_list"} disabled={saving} onChange={(v) => save(v)} />;
        break;
      }
      if (setting.widget === "choice_list") {
        control = (
          <ChoiceListEditor
            value={setting.value as Json[]}
            options={setting.options ?? []}
            disabled={saving}
            allowEmpty={(setting.default as Json[]).length === 0}
            onChange={(v) => save(v)}
          />
        );
        break;
      }
      if (setting.widget === "role_map") {
        control = <RoleMapEditor value={setting.value as Record<string, Json>} roles={roles} disabled={saving} onChange={(v) => save(v)} />;
        break;
      }
      if (setting.widget === "role_set") {
        control = <RoleSetEditor value={setting.value as Record<string, Json>} roles={roles} disabled={saving} onChange={(v) => save(v)} />;
        break;
      }
      control = (
        <textarea
          className="input min-h-32 font-mono text-xs leading-relaxed"
          spellCheck={false}
          value={draft as string}
         
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
            {setting.advanced && !compact && <Badge>Advanced</Badge>}
          </div>
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
        </div>
        <div className={setting.kind === "bool" ? "pt-0.5" : "w-full"}>{control}</div>
      </div>

      {error && <p className="mt-2 text-xs text-crimson">{error}</p>}

      {compact ? (
        !instant && numericDirty && (
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
          <span className="text-muted">{describeDefault(setting).slice(0, 120)}</span>
          {setting.updated_at && (
            <>
              {" "}· changed {utc(setting.updated_at)}
              {setting.updated_by ? ` by ${setting.updated_by.replace(/ \(\d+\)$/, "")}` : ""}
            </>
          )}
        </span>
        {!instant && numericDirty && (
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
        {setting.overridden && (
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
                  <button
                    className="text-gold hover:underline"
                    onClick={() => (h.old_value === null ? reset() : save(h.old_value))}
                    title="Set the value back to what it was before this change"
                  >
                    Revert
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

