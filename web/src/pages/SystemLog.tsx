import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  AlertOctagon,
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCopy,
  GitCommitHorizontal,
  Info,
  Pause,
  Play,
  Power,
  Search,
  Server,
  Skull,
} from "lucide-react";
import { ApiError, api, type LogLevel, type SystemEntry, type SystemLogMeta, type SystemLogPage } from "../lib/api";
import { formatDuration, useLive } from "../lib/live";
import { fullDateTime, shortDateTime, timeAgo, useNow } from "../lib/time";
import { Badge, Button, EmptyState, SectionHeader, Segmented, Skeleton, StatTile, cx } from "../components/ui";

const PAGE_SIZE = 50;

const VIEWS = [
  ["", "All"],
  ["lifecycle", "Bot events"],
  ["errors", "Errors"],
  ["warnings", "Warnings"],
] as const;
type View = (typeof VIEWS)[number][0];

const RANGES = [
  ["24h", "24h", 86400e3],
  ["7d", "7 days", 7 * 86400e3],
  ["30d", "30 days", 30 * 86400e3],
  ["all", "All", 0],
] as const;
type Range = (typeof RANGES)[number][0];

const LEVELS: Record<LogLevel, { label: string; tone: "neutral" | "info" | "warn" | "crimson"; icon: typeof Info }> = {
  DEBUG: { label: "Debug", tone: "neutral", icon: Info },
  INFO: { label: "Info", tone: "info", icon: Info },
  WARNING: { label: "Warning", tone: "warn", icon: AlertTriangle },
  ERROR: { label: "Error", tone: "crimson", icon: AlertOctagon },
  CRITICAL: { label: "Critical", tone: "crimson", icon: Skull },
};

const BOT_EVENTS: Record<string, string> = {
  "update.detected": "Update",
  "bot.stopping": "Stopping",
  "web.stopped": "Dashboard",
  "bot.started": "Starting",
  "web.started": "Dashboard",
  "bot.stopped": "Stopped",
  "bot.updated": "Updated",
  "bot.crashed": "Unexpected stop",
  "discord.connected": "Discord",
  "discord.outage": "Discord",
};

/** Where an entry came from, in words: "Bot", "Raid summary", "Discord library"… */
export function sourceLabel(entry: SystemEntry) {
  if (entry.category === "lifecycle") return BOT_EVENTS[entry.event] ?? "Bot";
  const name = entry.details.logger ?? entry.event;
  if (name === "wow-apply-bot") return "Bot core";
  if (name.startsWith("wow-apply-bot.")) {
    const part = name.slice("wow-apply-bot.".length).replace(/_/g, " ");
    return part.charAt(0).toUpperCase() + part.slice(1);
  }
  if (name.startsWith("discord")) return "Discord library";
  if (name.startsWith("aiohttp")) return "Web server";
  if (name.startsWith("asyncio")) return "Python";
  return name;
}

function matchesView(entry: SystemEntry, view: View, q: string) {
  if (view === "lifecycle" && entry.category !== "lifecycle") return false;
  if (view === "errors" && entry.level !== "ERROR" && entry.level !== "CRITICAL") return false;
  if (view === "warnings" && entry.level !== "WARNING") return false;
  const needle = q.trim().toLowerCase();
  return !needle || `${entry.message} ${entry.event} ${JSON.stringify(entry.details)}`.toLowerCase().includes(needle);
}

function LevelBadge({ level }: { level: LogLevel }) {
  const meta = LEVELS[level] ?? LEVELS.INFO;
  return (
    <Badge tone={meta.tone} className="shrink-0">
      <meta.icon className="h-2.5 w-2.5" />
      {meta.label}
    </Badge>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="sm"
      variant="ghost"
      icon={copied ? <Check className="h-3.5 w-3.5 text-ok" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
      onClick={() =>
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        })
      }
    >
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[100px_minmax(0,1fr)] gap-3 py-1">
      <dt className="text-2xs font-semibold uppercase tracking-wider text-faint">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function EntryDetails({ entry }: { entry: SystemEntry }) {
  const d = entry.details;
  const copyText = [entry.message, d.traceback].filter(Boolean).join("\n\n");
  return (
    <div className="space-y-2 text-[12.5px]">
      <dl>
        <Detail label={entry.count > 1 ? "First seen" : "When"}>
          {fullDateTime(entry.created_at)} <span className="text-faint">· {entry.created_at.replace("T", " ").replace("Z", " UTC")}</span>
        </Detail>
        {entry.count > 1 && (
          <Detail label="Last seen">
            {fullDateTime(entry.last_seen)} <span className="text-faint">· {entry.count} times in total</span>
          </Detail>
        )}
        {entry.category === "console" && (
          <Detail label="Source">
            {sourceLabel(entry)}{" "}
            <span className="font-mono text-2xs text-faint">
              {d.logger}
              {d.where ? ` · ${d.where}` : ""}
            </span>
          </Detail>
        )}
        {d.version && (
          <Detail label="Version">
            {d.commit_url ? (
              <a href={d.commit_url} target="_blank" rel="noreferrer" className="font-mono text-gold hover:underline">
                {d.version}
              </a>
            ) : (
              <span className="font-mono">{d.version}</span>
            )}
            {d.title && <span className="text-muted"> · {d.title}</span>}
          </Detail>
        )}
        {d.previous_version && (
          <Detail label="Previous">
            <span className="font-mono">{d.previous_version}</span>
            {d.compare_url && (
              <a href={d.compare_url} target="_blank" rel="noreferrer" className="ml-2 text-gold hover:underline">
                See every change
              </a>
            )}
          </Detail>
        )}
        {d.commits && d.commits.length > 0 && (
          <Detail label="Changes">
            <ul className="space-y-0.5">
              {d.commits.map((c) => (
                <li key={c.version} className="flex gap-2">
                  <GitCommitHorizontal className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" />
                  <span className="font-mono text-2xs text-faint">{c.version}</span>
                  <span>{c.title}</span>
                </li>
              ))}
            </ul>
          </Detail>
        )}
        {entry.event === "bot.crashed" && (
          <Detail label="What to do">
            <span className="text-muted">
              If this keeps happening, check the errors just before it. Out-of-memory kills and host reboots leave no
              error behind.
            </span>
          </Detail>
        )}
      </dl>
      {(d.traceback || entry.message.length > 120 || entry.message.includes("\n")) && (
        <div className="overflow-hidden rounded-lg border border-line bg-bg">
          <div className="flex items-center justify-between border-b border-line px-3 py-1">
            <span className="label">{d.traceback ? "Message and traceback" : "Message"}</span>
            <CopyButton text={copyText} />
          </div>
          <pre className="scrollbar-thin max-h-80 overflow-auto whitespace-pre p-3 font-mono text-2xs leading-relaxed text-muted">
            {copyText}
          </pre>
        </div>
      )}
    </div>
  );
}

function EntryRow({ entry, open, fresh, onToggle, now }: { entry: SystemEntry; open: boolean; fresh: boolean; onToggle: () => void; now: number }) {
  return (
    <li className={cx("transition-colors duration-1000", fresh ? "bg-gold/[0.07]" : open ? "bg-raised/40" : "")}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="grid w-full grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 px-4 py-2.5 text-left transition hover:bg-raised/50 md:grid-cols-[92px_96px_minmax(0,140px)_minmax(0,1fr)_auto_16px] md:items-center md:px-5"
      >
        <span className="order-1 flex min-w-0 items-center gap-2 md:order-2">
          <LevelBadge level={entry.level} />
          <span className="truncate text-xs text-muted md:hidden">{sourceLabel(entry)}</span>
        </span>
        <time
          dateTime={entry.last_seen}
          title={fullDateTime(entry.last_seen)}
          className="order-2 whitespace-nowrap text-right text-2xs tabular-nums text-faint md:order-1 md:text-left md:text-xs"
        >
          {shortDateTime(entry.last_seen, now)}
        </time>
        <span className="order-3 hidden truncate text-muted md:block">{sourceLabel(entry)}</span>
        <span className={cx("order-4 col-span-2 truncate md:col-span-1", entry.level === "ERROR" || entry.level === "CRITICAL" ? "text-fg" : "")}>
          {entry.message.split("\n")[0]}
        </span>
        <span className="order-5 hidden md:block">
          {entry.count > 1 && (
            <span className="rounded-md border border-line bg-raised px-1.5 py-px text-2xs tabular-nums text-muted" title={`Happened ${entry.count} times`}>
              ×{entry.count}
            </span>
          )}
        </span>
        <ChevronRight className={cx("order-6 hidden h-4 w-4 text-faint transition md:block", open && "rotate-90 text-gold")} />
        {entry.count > 1 && (
          <span className="order-5 col-span-2 text-2xs text-faint md:hidden">Happened {entry.count} times</span>
        )}
      </button>
      {open && (
        <div className="border-t border-line/60 bg-bg/40 px-4 py-3 md:px-5 md:pl-[124px]">
          <EntryDetails entry={entry} />
        </div>
      )}
    </li>
  );
}

function VersionCard({ meta, now }: { meta: SystemLogMeta; now: number }) {
  const update = meta.last_update;
  return (
    <section className="card mb-5 flex flex-wrap items-center gap-x-8 gap-y-3 px-5 py-4">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-gold/30 bg-gold/10 text-gold">
          <Server className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <div className="label">Running version</div>
          <div className="truncate">
            {meta.version.commit_url ? (
              <a href={meta.version.commit_url} target="_blank" rel="noreferrer" className="font-mono font-semibold text-gold hover:underline">
                {meta.version.version}
              </a>
            ) : (
              <span className="font-mono font-semibold">{meta.version.version}</span>
            )}
            {meta.version.title && <span className="text-muted"> · {meta.version.title}</span>}
          </div>
        </div>
      </div>
      <div className="text-xs">
        <div className="label">Last update</div>
        <div className="text-muted">
          {update ? (
            <>
              <span className="text-fg" title={fullDateTime(update.created_at)}>
                {timeAgo(update.created_at, now)}
              </span>
              {update.details.previous_version && <> from <span className="font-mono">{update.details.previous_version}</span></>}
            </>
          ) : (
            "None recorded yet"
          )}
        </div>
      </div>
      <div className="text-xs">
        <div className="label">Up for</div>
        <div className="text-fg">{formatDuration(meta.uptime_seconds)}</div>
      </div>
    </section>
  );
}

export default function SystemLogPage() {
  const [params, setParams] = useSearchParams();
  const view = (VIEWS.some(([id]) => id === params.get("view")) ? params.get("view") : "") as View;
  const range = (RANGES.some(([id]) => id === params.get("range")) ? params.get("range") : "all") as Range;
  const q = params.get("q") ?? "";
  const { subscribe } = useLive();
  const now = useNow();

  const [meta, setMeta] = useState<SystemLogMeta | null>(null);
  const [entries, setEntries] = useState<SystemEntry[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [live, setLive] = useState(true);
  const [queued, setQueued] = useState<SystemEntry[]>([]);
  const [search, setSearch] = useState(q);

  const setParam = useCallback(
    (updates: Record<string, string>) => {
      const next = new URLSearchParams(params);
      Object.entries(updates).forEach(([k, v]) => (v && !(k === "range" && v === "all") ? next.set(k, v) : next.delete(k)));
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  useEffect(() => setSearch(q), [q]);
  useEffect(() => {
    if (search === q) return;
    const t = window.setTimeout(() => setParam({ q: search.trim() }), 300);
    return () => window.clearTimeout(t);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadMeta = useCallback(() => {
    api.get<SystemLogMeta>("/api/system-log/meta").then(setMeta).catch(() => undefined);
  }, []);
  useEffect(loadMeta, [loadMeta]);

  const query = useCallback(
    (beforeId?: number) => {
      const qs = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (view) qs.set("view", view);
      if (q) qs.set("q", q);
      const ms = RANGES.find(([id]) => id === range)?.[2];
      if (ms) qs.set("since", new Date(Date.now() - ms).toISOString());
      if (beforeId) qs.set("before_id", String(beforeId));
      return `/api/system-log?${qs}`;
    },
    [view, q, range],
  );

  const requestId = useRef(0);
  useEffect(() => {
    const id = ++requestId.current;
    setEntries(null);
    setError(null);
    setQueued([]);
    setExpanded(new Set());
    api
      .get<SystemLogPage>(query())
      .then((page) => {
        if (id !== requestId.current) return;
        setEntries(page.entries);
        setHasMore(page.has_more);
      })
      .catch((e) => id === requestId.current && setError(e instanceof ApiError ? e.message : "Couldn't load the system log"));
  }, [query]);

  const loadMore = () => {
    if (!entries?.length) return;
    setLoadingMore(true);
    api
      .get<SystemLogPage>(query(entries[entries.length - 1].id))
      .then((page) => {
        setEntries((current) => [...(current ?? []), ...page.entries]);
        setHasMore(page.has_more);
      })
      .catch(() => setError("Couldn't load older entries"))
      .finally(() => setLoadingMore(false));
  };

  const flash = (id: number) => {
    setFresh((s) => new Set(s).add(id));
    window.setTimeout(
      () =>
        setFresh((s) => {
          const next = new Set(s);
          next.delete(id);
          return next;
        }),
      2500,
    );
  };

  // Live: new entries appear at the top; a repeat updates its row's count.
  const liveRef = useRef(live);
  liveRef.current = live;
  const filterRef = useRef({ view, q });
  filterRef.current = { view, q };
  const entriesRef = useRef(entries);
  entriesRef.current = entries;
  useEffect(
    () =>
      subscribe((message) => {
        if (message.type !== "system.entry") return;
        const entry = message.entry as SystemEntry;
        loadMeta();
        if (entriesRef.current?.some((e) => e.id === entry.id)) {
          setEntries((current) => current && current.map((e) => (e.id === entry.id ? entry : e)));
          flash(entry.id);
          return;
        }
        if (!matchesView(entry, filterRef.current.view, filterRef.current.q)) return;
        if (!liveRef.current) {
          setQueued((list) => [entry, ...list.filter((e) => e.id !== entry.id)]);
          return;
        }
        setEntries((current) => (current && !current.some((e) => e.id === entry.id) ? [entry, ...current] : current));
        flash(entry.id);
      }),
    [subscribe, loadMeta],
  );

  const showQueued = () => {
    setEntries((current) => {
      const known = new Set((current ?? []).map((e) => e.id));
      return [...queued.filter((e) => !known.has(e.id)), ...(current ?? [])];
    });
    setQueued([]);
  };

  const toggle = (id: number) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const views = useMemo(() => (meta && !meta.console_visible ? VIEWS.slice(0, 2) : VIEWS), [meta]);
  const filtered = Boolean(view || q || range !== "all");

  return (
    <>
      <SectionHeader
        eyebrow="System"
        title="System log"
        description="What happened to the bot itself: starts, stops, updates, and the warnings and errors its console shows."
        actions={
          <Button
            size="sm"
            variant={live ? "primary" : "outline"}
            onClick={() => {
              if (!live) showQueued();
              setLive(!live);
            }}
            icon={live ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            title={live ? "New entries appear as they happen. Click to pause." : "Paused. Click to show new entries as they happen."}
          >
            {live ? "Live" : "Paused"}
          </Button>
        }
      />

      {meta ? <VersionCard meta={meta} now={now} /> : <Skeleton className="mb-5 h-[70px]" />}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {meta ? (
          <>
            <StatTile value={meta.console_visible ? meta.errors_24h : "—"} label="Errors, last 24h" icon={<AlertOctagon className="h-3 w-3" />} tone={meta.errors_24h ? "crimson" : "ok"} />
            <StatTile value={meta.console_visible ? meta.warnings_24h : "—"} label="Warnings, last 24h" icon={<AlertTriangle className="h-3 w-3" />} tone={meta.warnings_24h ? "warn" : undefined} />
            <StatTile value={meta.starts_7d} label="Starts, last 7 days" icon={<Power className="h-3 w-3" />} />
            <StatTile value={meta.crashes_7d} label="Unexpected stops, 7 days" icon={<Skull className="h-3 w-3" />} tone={meta.crashes_7d ? "crimson" : undefined} />
          </>
        ) : (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[76px]" />)
        )}
      </div>

      {meta && !meta.console_visible && (
        <div className="mb-5 flex items-center gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-muted">
          <Info className="h-4 w-4 shrink-0 text-info" />
          Console warnings and errors are only shown to admins: they can contain technical details about members and the
          server. You can see the bot's starts, stops and updates.
        </div>
      )}

      <section className="card">
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3 md:px-5 md:py-4">
          <Segmented label="Show" value={view} options={views} onChange={(v) => setParam({ view: v })} />
          <div className="relative min-w-[200px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
            <input
              className="input pl-9"
              placeholder="Search messages and tracebacks…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search the system log"
            />
          </div>
          <Segmented label="Time range" value={range} options={RANGES} onChange={(r) => setParam({ range: r })} />
        </div>

        {queued.length > 0 && (
          <button
            type="button"
            onClick={showQueued}
            className="flex w-full items-center justify-center gap-1.5 border-b border-line bg-gold/[0.06] py-2 text-xs font-medium text-gold transition hover:bg-gold/10"
          >
            Show {queued.length} new entr{queued.length === 1 ? "y" : "ies"}
          </button>
        )}

        <div className="hidden grid-cols-[92px_96px_minmax(0,140px)_minmax(0,1fr)_auto_16px] gap-x-3 border-b border-line px-5 py-2 md:grid">
          {["Time", "Level", "Source", "Message", "", ""].map((h, i) => (
            <div key={i} className="label">
              {h}
            </div>
          ))}
        </div>

        {error ? (
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load the system log">
            {error}
          </EmptyState>
        ) : entries === null ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : entries.length === 0 ? (
          <EmptyState icon={<Server className="h-6 w-6" />} title={filtered ? "Nothing matches" : "Nothing recorded yet"}>
            {view === "errors"
              ? "No errors. The bot has been running cleanly."
              : filtered
                ? "Try a longer time range or another view."
                : "Entries appear here when the bot starts, stops, updates, or logs a warning or error."}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {entries.map((entry) => (
              <EntryRow key={entry.id} entry={entry} open={expanded.has(entry.id)} fresh={fresh.has(entry.id)} onToggle={() => toggle(entry.id)} now={now} />
            ))}
          </ul>
        )}

        {entries && entries.length > 0 && (
          <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3 text-2xs text-faint md:px-5">
            <span>
              {meta?.capture_level === "OFF"
                ? "Console output isn't being kept"
                : `Keeping console ${(meta?.capture_level ?? "WARNING").toLowerCase()}s and above`}
              {meta && meta.retention_days > 0 && ` for ${meta.retention_days} days`}.{" "}
              <Link to="/settings?group=logging" className="text-gold hover:underline">
                Change
              </Link>
            </span>
            {hasMore ? (
              <Button size="sm" onClick={loadMore} loading={loadingMore} icon={<ChevronDown className="h-3.5 w-3.5" />}>
                Load older
              </Button>
            ) : (
              <span>That's everything{filtered ? " matching these filters" : ""}.</span>
            )}
          </footer>
        )}
      </section>
    </>
  );
}
