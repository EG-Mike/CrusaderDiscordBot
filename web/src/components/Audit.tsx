// Building blocks shared by the Audit log page and the Overview activity feed.
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Bot,
  Check,
  ChevronsUp,
  ClipboardCopy,
  Coins,
  FormInput,
  Hammer,
  Hash,
  KeyRound,
  ListChecks,
  LogIn,
  Megaphone,
  MessageSquareText,
  MonitorCog,
  MousePointerClick,
  Rocket,
  ScrollText,
  Settings2,
  Smile,
  Sparkles,
  SquareMousePointer,
  SquareSlash,
  Users,
  Vote,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { AuditEvent, AuditKind, Json } from "../lib/api";
import { fullDateTime } from "../lib/time";
import { Badge, cx } from "./ui";

type Tone = "neutral" | "gold" | "crimson" | "ok" | "warn" | "info";

export const KINDS: Record<AuditKind, { label: string; tone: Tone; icon: LucideIcon }> = {
  command: { label: "Command", tone: "gold", icon: SquareSlash },
  context_menu: { label: "Context menu", tone: "gold", icon: SquareMousePointer },
  button: { label: "Button", tone: "info", icon: MousePointerClick },
  select: { label: "Dropdown", tone: "info", icon: ListChecks },
  modal: { label: "Form", tone: "neutral", icon: FormInput },
  login: { label: "Login", tone: "ok", icon: LogIn },
  setting: { label: "Setting", tone: "warn", icon: Settings2 },
  integration: { label: "API key", tone: "crimson", icon: KeyRound },
  setup: { label: "Setup", tone: "ok", icon: Rocket },
  bot: { label: "Bot", tone: "crimson", icon: Bot },
  announcement: { label: "Announcement", tone: "gold", icon: Megaphone },
  bot_message: { label: "Bot message", tone: "info", icon: MessageSquareText },
  emoji: { label: "Emoji", tone: "gold", icon: Smile },
  maintenance: { label: "Maintenance", tone: "warn", icon: Wrench },
  poll: { label: "Poll", tone: "info", icon: Vote },
  roster: { label: "Roster", tone: "gold", icon: Users },
  crafting: { label: "Crafting", tone: "info", icon: Hammer },
  auction: { label: "Auction prices", tone: "gold", icon: Coins },
  rank_sync: { label: "Rank sync", tone: "info", icon: ChevronsUp },
  audit: { label: "Audit log", tone: "neutral", icon: ScrollText },
  other: { label: "Other", tone: "neutral", icon: Sparkles },
};

export function KindBadge({ kind }: { kind: AuditKind }) {
  const meta = KINDS[kind] ?? KINDS.other;
  return (
    <Badge tone={meta.tone} className="shrink-0">
      <meta.icon className="h-2.5 w-2.5" />
      {meta.label}
    </Badge>
  );
}

export function Avatar({ url, name, size = "h-6 w-6" }: { url: string | null; name: string | null; size?: string }) {
  if (url) return <img src={url} alt="" className={cx(size, "shrink-0 rounded-full ring-1 ring-line")} loading="lazy" />;
  return (
    <span
      className={cx(
        size,
        "flex shrink-0 items-center justify-center rounded-full bg-raised text-2xs font-semibold uppercase text-muted ring-1 ring-line",
      )}
      aria-hidden
    >
      {(name ?? "?").slice(0, 1)}
    </span>
  );
}

/** Where it happened: "#channel", "DM" or "Dashboard". */
export function Where({ event, className }: { event: AuditEvent; className?: string }) {
  if (event.source === "dashboard")
    return (
      <span className={cx("inline-flex min-w-0 items-center gap-1 text-muted", className)}>
        <MonitorCog className="h-3 w-3 shrink-0 text-faint" /> Dashboard
      </span>
    );
  if (!event.channel_id) return <span className={cx("text-muted", className)}>{event.channel_name ?? "DM"}</span>;
  return (
    <span className={cx("inline-flex min-w-0 items-center gap-0.5 text-muted", className)}>
      <Hash className="h-3 w-3 shrink-0 text-faint" />
      <span className="truncate">{event.channel_name ?? event.channel_id}</span>
    </span>
  );
}

export function discordUrl(guildId: string | undefined, channelId: string | null, messageId?: string | null) {
  if (!guildId || !channelId) return null;
  return `https://discord.com/channels/${guildId}/${channelId}${messageId ? `/${messageId}` : ""}`;
}

function CopyId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        });
      }}
      className="inline-flex items-center gap-1 rounded font-mono text-2xs text-faint transition hover:text-fg"
      title="Copy ID"
    >
      {value}
      {copied ? <Check className="h-3 w-3 text-ok" /> : <ClipboardCopy className="h-3 w-3" />}
    </button>
  );
}

function show(value: Json | undefined): string {
  if (value === null || value === undefined || value === "") return "(empty)";
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-3 py-1">
      <dt className="text-2xs font-semibold uppercase tracking-wider text-faint">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** Everything known about one event, shown when its row is expanded. */
export function EventDetails({ event, guildId }: { event: AuditEvent; guildId?: string }) {
  const d = event.details;
  const messageId = (d.message_id as string | undefined) ?? null;
  const messageUrl = discordUrl(guildId, event.channel_id, messageId);
  const channelUrl = discordUrl(guildId, event.channel_id);
  const options = d.options && typeof d.options === "object" && !Array.isArray(d.options) ? (d.options as Record<string, Json>) : null;

  return (
    <dl className="text-[12.5px]">
      <Field label="When">
        {fullDateTime(event.created_at)} <span className="text-faint">· {event.created_at.replace("T", " ").replace("Z", " UTC")}</span>
      </Field>
      <Field label="Member">
        <span className="mr-2">
          {event.actor_name ?? "Unknown"}
          {typeof d.username === "string" && d.username !== event.actor_name && <span className="text-faint"> @{d.username}</span>}
        </span>
        {event.actor_id && <CopyId value={event.actor_id} />}
      </Field>
      {event.source === "discord" && (
        <Field label="Channel">
          {channelUrl ? (
            <a href={channelUrl} target="_blank" rel="noreferrer" className="text-gold hover:underline">
              #{event.channel_name ?? event.channel_id}
            </a>
          ) : (
            event.channel_name ?? "DM"
          )}
        </Field>
      )}

      {event.kind === "setting" && (
        <>
          <Field label="Setting">
            <Link to={`/settings?q=${encodeURIComponent(String(d.key ?? event.target ?? ""))}#${d.key ?? event.target}`} className="text-gold hover:underline">
              {String(d.label ?? event.target)}
            </Link>{" "}
            <span className="font-mono text-2xs text-faint">{String(d.key ?? event.target ?? "")}</span>
          </Field>
          {"old" in d && (
            <Field label="Change">
              <span className="font-mono text-2xs">
                <span className="text-crimson/90 line-through decoration-crimson/40">{String(d.old_name ?? show(d.old))}</span>
                <span className="px-1.5 text-faint">→</span>
                <span className="text-ok">{String(d.new_name ?? show(d.new))}</span>
              </span>
            </Field>
          )}
        </>
      )}

      {options && Object.keys(options).length > 0 && (
        <Field label="Options">
          <div className="flex flex-wrap gap-1">
            {Object.entries(options).map(([k, v]) => (
              <span key={k} className="rounded border border-line bg-bg px-1.5 py-px font-mono text-2xs">
                <span className="text-faint">{k}=</span>
                {show(v)}
              </span>
            ))}
          </div>
        </Field>
      )}
      {typeof d.options_text === "string" && <Field label="Options"><span className="font-mono text-2xs">{d.options_text}</span></Field>}
      {Array.isArray(d.values) && <Field label="Picked">{d.values.map(show).join(", ")}</Field>}
      {Array.isArray(d.fields) && (
        <Field label="Fields">
          <span className="text-muted">{d.fields.map(show).join(", ")}</span>{" "}
          <span className="text-faint">(what was typed is never recorded)</span>
        </Field>
      )}
      {messageUrl && messageId && (
        <Field label="Message">
          <a href={messageUrl} target="_blank" rel="noreferrer" className="text-gold hover:underline">
            Open in Discord
          </a>
        </Field>
      )}
      {event.kind === "context_menu" && event.target && (
        <Field label="Used on">
          <CopyId value={event.target} />
        </Field>
      )}
      {event.kind === "audit" && typeof d.rows === "number" && <Field label="Rows">{d.rows}</Field>}
      {typeof d.custom_id === "string" && (
        <Field label="Component ID">
          <span className="font-mono text-2xs text-faint">{d.custom_id}</span>
        </Field>
      )}
      {event.source === "dashboard" && (
        <Field label="Action">
          <span className="font-mono text-2xs text-faint">{event.action}</span>
        </Field>
      )}
    </dl>
  );
}
