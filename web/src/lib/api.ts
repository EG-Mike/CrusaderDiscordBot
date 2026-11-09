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
  /** The guild's name and logo as set in Settings (falls back to the Discord server's). */
  branding: Branding;
  /** Link templates ("https://...{id}") for items and spells, and whether Wowhead's tooltips are on. */
  links: { item: string | null; spell: string | null; wowhead_tooltips: boolean };
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

export interface Branding {
  name: string | null;
  logo_url: string | null;
  custom_logo: boolean;
}

export interface Setting {
  key: string;
  label: string;
  group: string;
  kind: SettingKind;
  options: { value: string | number; label: string }[] | null;
  help: string;
  /** Only shown with "Show advanced settings" on. */
  advanced: boolean;
  /** A friendlier editor than raw JSON. */
  widget: "text_list" | "number_list" | "role_map" | "role_set" | "choice_list" | null;
  /** Shown after a number field, e.g. "minutes". */
  unit: string | null;
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
  section: string;
  label: string;
  description: string;
  count: number;
  advanced_count: number;
}

/** One entry in the Settings menu: one group, or several shown as subsections. */
export interface SettingTab {
  id: string;
  section: string;
  label: string;
  description: string;
  groups: string[];
  count: number;
  advanced_count: number;
}

export interface SettingSection {
  id: string;
  label: string;
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
  | "maintenance"
  | "poll"
  | "roster"
  | "crafting"
  | "auction"
  | "rank_sync"
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

// --- polls ---------------------------------------------------------------------

export type PollPhase = "adding" | "voting" | "closed";

export interface PollSettings {
  members_can_add: boolean;
  max_adds: number;
  collect_first: boolean;
  max_votes: number;
  live_results: boolean;
  show_authors: boolean;
  show_voters: boolean;
  allow_vote_change: boolean;
}

export interface Poll {
  message_id: string;
  channel_id: string;
  channel_name: string | null;
  question: string;
  description: string | null;
  phase: PollPhase;
  created_by: string;
  created_by_name: string | null;
  closes_at: number | null; // unix seconds
  duration: number | null;
  voter_count: number;
  results_public: boolean;
  settings: PollSettings;
  options: { id: number; text: string; votes: number; author_name: string | null; voters: string[] }[];
}

export interface PollList {
  polls: Poll[];
  defaults: Omit<PollSettings, "members_can_add">;
  feature_enabled: boolean;
}

// --- guild roster & leveling (webapi/roster_api.py) -----------------------------

export interface RosterMember {
  id: string;
  name: string;
  avatar_url: string;
}

export interface RosterCharacter {
  slug: string;
  name: string;
  surname: string | null;
  display_name: string;
  class_id: number | null;
  race_id: number | null;
  faction: string | null;
  level: number | null;
  ruleset_id: number | null;
  guild_slug: string | null;
  guild_rank: number | null;
  discord_id: string | null;
  member: RosterMember | null;
  is_main: boolean;
  main_spec_id: number | null;
  off_spec_id: number | null;
  /** Milliseconds since 1970, from Blizzard. */
  last_login: number | null;
  /** 1: Blizzard has a public profile, 0: not (below level 10), null: not checked yet. */
  profile_found: number | null;
  updated_at: string;
}

export interface WowClass {
  id: number;
  name: string;
  specs: { id: number; name: string; role: string | null }[];
}

export interface RosterOverview {
  guild_name: string | null;
  self_service: boolean;
  last_sync: { at: string; ok: boolean; members?: number | null; message: string | null } | null;
  characters: RosterCharacter[];
  classes: WowClass[];
  rulesets: { id: number; name: string }[];
  /** The roster posted in Discord (Settings: roster channel), or null when no channel is set. */
  post: { channel_id: string; channel_name: string; updated: number | null } | null;
}

export interface LevelEvent {
  id: number;
  created_at: string;
  old_level: number | null;
  new_level: number | null;
  milestone: number | null;
  /** "announced", "alt", "too old", "no channel", "no milestone", or null while waiting. */
  outcome: string | null;
  announced_at: string | null;
  slug: string | null;
  display_name: string;
  class_id: number | null;
  member: RosterMember | null;
}

export interface LevelingOverview {
  enabled: boolean;
  channel: { id: string; name: string } | null;
  milestones: number[];
  announce_alts: boolean;
  refresh_minutes: number;
  leaderboard: RosterCharacter[];
  events: LevelEvent[];
}

// --- character lookup (webapi/lookup_api.py) --------------------------------------

export interface GearItem {
  slot: string;
  slot_name: string;
  item_id: number | null;
  name: string;
  quality: string | null;
  item_level: number | null;
  stats: string[];
  enchants: string[];
  sockets: { type: string | null; gem: string | null; text: string | null }[];
  set: string | null;
  icon_url: string | null;
  /** The item's page (Settings: Item link), or null. */
  url?: string | null;
}

export interface CharacterSummary {
  character: string;
  slug: string;
  level: number | null;
  class_id: number | null;
  class_name: string;
  race: string | null;
  faction: string | null;
  guild: string | null;
  ruleset_id: number | null;
  ruleset: string | null;
  average_item_level: number | null;
  equipped_item_level: number | null;
  last_login: number | null;
  items: GearItem[];
  talents: { spec: string; spec_id: number | null; points: number }[];
  professions: { name: string; skill: number | null; max: number | null; primary: boolean }[];
  empty_slots: string[];
  missing_enchants: string[];
  empty_sockets: number;
  profile_updated: string | null;
  stats?: Record<string, number>;
}

export interface StatValue {
  key: string;
  label: string;
  value: number;
  text: string;
}

/** The chosen achievement statistics (e.g. raid boss kills), grouped per instance as on the card. */
export interface CardStatistics {
  title: string | null;
  groups: { group: string | null; items: { label: string; value: number }[] }[];
}

export interface CardStatistic {
  id: number;
  name: string;
  category: string | null;
  label: string;
  group: string | null;
  chosen?: boolean;
}

export interface CardStatisticsConfig {
  title: string | null;
  chosen: CardStatistic[];
  catalog: CardStatistic[];
  can_scan: boolean;
}

export interface StatGroup {
  title: string;
  stats: StatValue[];
}

export interface LookupResult {
  character: CharacterSummary;
  off_ruleset: boolean;
  requirement_problems: string[];
  roster: { on_roster: boolean; in_guild: boolean; is_main: boolean; member: RosterMember | null } | null;
  stat_groups: StatGroup[];
  statistics: CardStatistics;
  warcraftlogs: null;
}

// --- applications (webapi/applications_api.py) ------------------------------------

export type ApplicationStatus = "pending" | "approved" | "denied";

export interface ApplicationRow {
  id: string;
  status: ApplicationStatus;
  character: string;
  character_slug: string | null;
  class_name: string | null;
  level: number | null;
  item_level: number | null;
  guild: string | null;
  role: string | null;
  spec: string | null;
  offspec: string | null;
  applicant: { id: string; name: string | null; username: string | null; avatar_url: string | null };
  applicant_in_server: boolean;
  /** Unix seconds. */
  submitted_at: number;
  has_gear: boolean;
  flag_count: number;
  has_note: boolean;
  screenshot_count: number;
  prior_denials: number;
  archived: boolean;
  decided_by: string | null;
  decided_at: number | null;
}

export interface ApplicationList {
  applications: ApplicationRow[];
  counts: Record<ApplicationStatus, number>;
  running: boolean;
  can_decide: boolean;
  archive_problem: string | null;
}

export interface ApplicationDecision {
  status: "approved" | "denied";
  by_id: string;
  by_name: string;
  via: "discord" | "dashboard";
  at: number;
  roles_assigned: string[];
  roles_kept: string[];
  nickname: string | null;
  roster: string | null;
  reason: string | null;
}

export interface ApplicationDetail extends ApplicationRow {
  note: string | null;
  screenshots: string[];
  gear: (CharacterSummary & { stat_highlights: StatValue[]; stat_groups: StatGroup[] }) | null;
  gear_checked_at: number | null;
  flags: string[];
  requirement_problems: string[];
  profile_url: string | null;
  wcl_url: string | null;
  message_url: string | null;
  decision: ApplicationDecision | null;
  decision_text: string | null;
  legacy_text: string | null;
  statistics: CardStatistics;
  warcraftlogs: { enabled: boolean; text: string | null };
}

// --- crafting (webapi/crafting_api.py) ----------------------------------------------

export interface Crafter {
  slug: string;
  display_name: string;
  class_name: string | null;
  level: number | null;
  in_guild: boolean;
  is_main: boolean;
  member: { id: string; name: string; avatar_url: string } | null;
  skill?: number | null;
  max?: number | null;
}

export interface CraftingProfession {
  id: number;
  name: string;
  icon_url: string | null;
  crafters: Crafter[];
}

export interface FeaturedRecipe {
  recipe_id: number;
  name: string;
  profession_id: number | null;
  profession_name: string | null;
  item_id: number | null;
  item_name: string | null;
  quantity: number | null;
  reagents: { id: number | null; name: string; quantity: number | null }[];
  icon_url: string | null;
  item_url: string | null;
  item_icon_url: string | null;
  added_by: string | null;
  added_at: string;
  category: string | null;
  /** 0 = easiest .. 1 = hardest in its category (Blizzard's list order). */
  difficulty: number | null;
  /** A moderator's place for it in its profession; null = hardest first. */
  position: number | null;
  /** Left out of the channel: nobody knows it and the setting hides those. */
  hidden_in_channel: boolean;
  crafters: Crafter[];
}

export interface CraftingOverview {
  professions: CraftingProfession[];
  featured: FeaturedRecipe[];
  /** Professions whose featured recipes a moderator ordered by hand. */
  custom_order: number[];
  hide_unknown_recipes: boolean;
  characters: number;
  characters_with_recipes: number;
  channel_problem: string | null;
}

export interface RecipeCatalog {
  id: number;
  name: string;
  categories: { name: string; recipes: { id: number; name: string; known_by: number; featured: boolean }[] }[];
}

// --- auction prices (webapi/auction_api.py) -------------------------------------------

/** Prices are copper per item; the median is the middle item of all for sale. */
export interface AuctionPrice {
  min_price: number;
  median_price: number;
  quantity: number;
  listings: number;
}

export interface AuctionItem {
  id: number;
  name: string;
  quality: string | null;
  url: string | null;
  price?: AuctionPrice | null;
}

export interface AuctionSnapshot {
  snapshot_at: string | null;
  checked_at: string;
  auctions: number;
  items: number;
}

export interface AuctionOverview {
  ruleset_id: number;
  house_id: number | null;
  house_name: string | null;
  snapshot: AuctionSnapshot | null;
  problem: string | null;
  chat_channel: { id: string; name: string } | null;
  triggers: string[];
  trigger_after_item: boolean;
  answer_in: "dm" | "channel";
  answer_channel: { id: string; name: string } | null;
}

export interface AuctionTry {
  items: string[];
  answer: string | null;
  problem: string | null;
}

export interface AuctionHouses {
  ruleset_id: number;
  rulesets: { id: number; name: string }[];
  houses: { id: number; name: string }[];
}

export interface AuctionComparison {
  item: AuctionItem;
  houses: ({ house_id: number; quantity: number; listings: number; min_price: number | null; median_price: number | null })[];
}

// --- rank sync (webapi/rank_sync_api.py) -----------------------------------------------

export type RankSyncStatus = "promote" | "unknown_rank" | "not_in_guild" | "no_character" | "ok" | "no_rule_role" | "not_in_server";

export interface RankSyncEntry {
  member: { id: string; name: string | null; avatar_url: string | null; role_names: string[] };
  role_name: string | null;
  target_rank: number | null;
  target_label: string | null;
  status: RankSyncStatus;
  character: {
    slug: string;
    display_name: string;
    class_id: number | null;
    level: number | null;
    in_guild: boolean;
    guild_rank: number | null;
    rank_label: string | null;
  } | null;
}

export interface RankSyncOverview {
  guild_name: string | null;
  rules?: { role_id: string; role_name: string | null; rank: number; rank_label: string }[];
  ranks?: { rank: number; name: string | null; count: number; examples: string[] }[];
  max_rank?: number;
  entries?: RankSyncEntry[];
  suggested_role_id?: string | null;
  classes?: WowClass[];
  post?: { channel_id: string; channel_name: string; updated: number | null; problem: string | null } | null;
}
