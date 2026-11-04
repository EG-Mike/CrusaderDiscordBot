// Thin fetch wrapper for the bot's dashboard API (webapi/server.py).
// Every write sends X-Crusader-Request: the server's CSRF check.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-Crusader-Request": "1" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, (data as { error?: string }).error ?? res.statusText);
  return data as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  del: <T,>(path: string) => request<T>("DELETE", path),
};

// --- API types -------------------------------------------------------------

export type Level = "moderator" | "approver" | "admin";

export interface Me {
  id: string;
  username: string;
  avatar_url: string;
  levels: Level[];
}

export interface Status {
  online: boolean;
  latency_ms: number | null;
  uptime_seconds: number;
  version: string;
  version_info: { title: string | null; built_at: string | null; commit_url: string | null };
  system: {
    errors_24h: number;
    warnings_24h: number;
    crashes_7d: number;
    last_error_at: string | null;
    last_update: { at: string; previous_version: string | null } | null;
  };
  environment: string | null;
  bot: { name: string | null; avatar_url: string | null };
  guild: { id: string; name: string; icon_url: string | null; member_count: number } | null;
  features: Record<string, boolean>;
  feature_labels: Record<string, string>;
  counts: { pending_applications: number };
  missing_setup: MissingSetup[];
  setup_completed: boolean;
  restart_available: boolean;
  pending_restart: string[];
  warnings: string[];
}

export type SettingKind = "bool" | "int" | "float" | "string" | "text" | "enum" | "role" | "channel" | "json";
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Setting {
  key: string;
  label: string;
  group: string;
  kind: SettingKind;
  options: string[] | null;
  help: string;
  read_only: boolean;
  read_only_reason: string | null;
  value: Json;
  running_value: Json;
  default: Json;
  default_source: "deployment" | "local" | "env";
  overridden: boolean;
  updated_at: string | null;
  updated_by: string | null;
  pending_restart: boolean;
  /** Applied to the running bot as soon as it's saved (no restart). */
  live: boolean;
}

export interface SettingGroup {
  id: string;
  label: string;
  description: string;
  count: number;
}

export interface HistoryEntry {
  id: number;
  old_value: Json;
  new_value: Json;
  changed_at: string;
  changed_by: string | null;
}

export interface Channel {
  id: string;
  name: string;
  type: string;
  category: string | null;
  bot_can_view: boolean;
  bot_can_send: boolean;
}

export interface Role {
  id: string;
  name: string;
  color: string | null;
  managed: boolean;
}

export interface MissingSetup {
  key: string;
  label: string;
  group: string;
}

export interface SecretField {
  key: string;
  service: string;
  label: string;
  help: string;
  set: boolean;
  source: "dashboard" | "env" | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface Service {
  id: string;
  label: string;
  required: boolean;
  configured: boolean;
  fields: SecretField[];
}

export interface SetupState {
  completed: boolean;
  missing: MissingSetup[];
  checks: { label: string; ok: boolean }[];
  guild: { name: string; icon_url: string | null } | null;
  restart_available: boolean;
}

// --- audit log -----------------------------------------------------------------

export type AuditSource = "discord" | "dashboard";
export type AuditKind =
  | "command"
  | "context_menu"
  | "button"
  | "select"
  | "modal"
  | "login"
  | "setting"
  | "integration"
  | "setup"
  | "bot"
  | "announcement"
  | "bot_message"
  | "emoji"
  | "audit"
  | "other";

export interface AuditEvent {
  id: number;
  created_at: string; // ISO 8601, UTC
  source: AuditSource;
  kind: AuditKind;
  action: string;
  summary: string;
  actor_id: string | null;
  actor_name: string | null;
  actor_avatar_url: string | null;
  channel_id: string | null;
  channel_name: string | null;
  target: string | null;
  details: Record<string, Json>;
}

export interface AuditPage {
  events: AuditEvent[];
  has_more: boolean;
}

export interface AuditMeta {
  actors: { id: string; name: string | null; count: number; avatar_url: string | null }[];
  channels: { id: string; name: string | null; count: number }[];
  kinds: { kind: AuditKind; count: number }[];
  stats: {
    events_24h: number;
    events_7d: number;
    members_7d: number;
    dashboard_7d: number;
    total: number;
    oldest: string | null;
    retention_days: number;
  };
  discord_capture_enabled: boolean;
}

// --- system log ----------------------------------------------------------------

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export interface SystemEntry {
  id: number;
  created_at: string; // first seen, ISO 8601 UTC
  last_seen: string;
  count: number;
  level: LogLevel;
  category: "lifecycle" | "console";
  event: string; // e.g. "bot.updated", or the logger name for console output
  message: string;
  details: {
    logger?: string;
    where?: string;
    traceback?: string;
    version?: string;
    previous_version?: string;
    title?: string | null;
    commit_url?: string | null;
    compare_url?: string | null;
    commits?: { version: string; title: string }[];
    last_heartbeat?: string | null;
    [key: string]: Json | undefined;
  };
}

export interface SystemLogPage {
  entries: SystemEntry[];
  has_more: boolean;
}

export interface SystemLogMeta {
  errors_24h: number;
  warnings_24h: number;
  starts_7d: number;
  crashes_7d: number;
  last_update: SystemEntry | null;
  last_error_at: string | null;
  capture_level: string;
  retention_days: number;
  console_visible: boolean;
  version: {
    version: string;
    title: string | null;
    built_at: string | null;
    repository: string | null;
    commit_url: string | null;
  };
  uptime_seconds: number;
}

// --- announcements ---------------------------------------------------------------

export interface Announcement {
  id: number;
  status: "draft" | "published";
  title: string | null;
  body: string;
  color: string | null;
  ping: string | null; // "everyone" | "here" | "role:<id>"
  channel_id: string | null;
  channel_name: string | null;
  message_id: string | null;
  sandbox_message_id: string | null;
  revision: number;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  updated_by: string | null;
  published_at: string | null;
  published_by: string | null;
}

export interface AnnouncementVersion {
  id: number;
  event: "published" | "edited";
  title: string | null;
  body: string;
  color: string | null;
  saved_at: string;
  saved_by: string | null;
}

export interface AnnouncementList {
  announcements: Announcement[];
  counts: { draft: number; published: number };
  feature_enabled: boolean;
}

export interface AnnouncementTemplate {
  id: number;
  name: string;
  title: string | null;
  body: string;
  color: string | null;
  ping: string | null;
  updated_at: string;
  updated_by: string | null;
}

// --- bot messages ------------------------------------------------------------------

export interface BotMessage {
  key: string;
  label: string;
  group: string;
  description: string;
  kind: "embed" | "text";
  where: string;
  title: string | null;
  body: string;
  color: string | null;
  customized: boolean;
  updated_at: string | null;
  updated_by: string | null;
  default: { title: string | null; body: string; color: string | null };
  default_color: string;
  footer: string | null;
  variables: { name: string; description: string; value: string; from_settings: boolean }[];
  can_update_discord: boolean;
  feature_enabled: boolean;
  limit: number;
}

export interface BotMessageSaveResult {
  message: BotMessage;
  discord?: string;
  discord_error?: string;
  unknown_placeholders?: string[];
}

export interface BotMessageVersion {
  id: number;
  title: string | null;
  body: string;
  color: string | null;
  saved_at: string;
  saved_by: string | null;
}

// --- emoji ------------------------------------------------------------------------

export interface EmojiAlias {
  kind: "item" | "spell";
  game_id: number;
  name: string;
}

export interface LibraryEmoji {
  id: number;
  emoji_id: string;
  name: string;
  animated: boolean;
  text: string; // how to write it in a message, e.g. <:inv_sword_39:123>
  url: string;
  file_id: number | null;
  icon_name: string | null;
  source: string | null;
  created_at: string;
  created_by: string | null;
  aliases: EmojiAlias[];
}

export interface PlainEmoji {
  emoji_id: string;
  name: string;
  animated: boolean;
  url: string;
  text: string;
  available?: boolean;
}

export interface EmojiOverview {
  library: LibraryEmoji[];
  other: PlainEmoji[];
  server: PlainEmoji[];
  used: number;
  limit: number;
  can_manage: boolean;
  blizzard_configured: boolean;
}

export interface EmojiSearchResult {
  kind: "item" | "spell";
  game_id: number;
  name?: string;
  image_url?: string;
  file_id?: number | null;
  icon_name?: string | null;
  source?: string;
  state?: "new" | "shared" | "added";
  emoji?: LibraryEmoji;
  error?: string;
}

export interface EmojiAddResult {
  kind: "item" | "spell";
  game_id: number;
  status: "created" | "shared" | "exists" | "error";
  emoji?: LibraryEmoji;
  item?: EmojiSearchResult;
  error?: string;
}
