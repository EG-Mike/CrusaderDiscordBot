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
  environment: string | null;
  bot: { name: string | null; avatar_url: string | null };
  guild: { id: string; name: string; icon_url: string | null; member_count: number } | null;
  features: Record<string, boolean>;
  counts: { pending_applications: number };
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
  default_source: "deployment" | "local";
  overridden: boolean;
  updated_at: string | null;
  updated_by: string | null;
  pending_restart: boolean;
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
