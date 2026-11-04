import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  Download,
  Filter,
  MonitorCog,
  Pause,
  Play,
  ScrollText,
  Search,
  Users,
  X,
} from "lucide-react";
import { ApiError, api, type AuditEvent, type AuditKind, type AuditMeta, type AuditPage } from "../lib/api";
import { useLive } from "../lib/live";
import { fullDateTime, shortDateTime, useNow } from "../lib/time";
import { Avatar, EventDetails, KINDS, KindBadge, Where } from "../components/Audit";
import { Button, EmptyState, SectionHeader, Segmented, Skeleton, StatTile, cx } from "../components/ui";

const PAGE_SIZE = 50;

const RANGES = [
  ["24h", "24h", 86400e3],
  ["7d", "7 days", 7 * 86400e3],
  ["30d", "30 days", 30 * 86400e3],
  ["all", "All", 0],
] as const;
type Range = (typeof RANGES)[number][0];

const SOURCES = [
  ["", "Everywhere"],
  ["discord", "Discord"],
  ["dashboard", "Dashboard"],
] as const;

interface Filters {
  q: string;
  source: string;
  kind: string;
  actor: string;
  channel: string;
  range: Range;
}

function readFilters(params: URLSearchParams): Filters {
  const range = params.get("range") as Range | null;
  return {
    q: params.get("q") ?? "",
    source: params.get("source") ?? "",
    kind: params.get("kind") ?? "",
    actor: params.get("actor") ?? "",
    channel: params.get("channel") ?? "",
    range: range && RANGES.some(([id]) => id === range) ? range : "all",
  };
}

/** Filters -> the API's query string (the same one the CSV export takes). */
function toQuery(f: Filters) {
  const qs = new URLSearchParams();
  if (f.q) qs.set("q", f.q);
  if (f.source) qs.set("source", f.source);
  if (f.kind) qs.set("kind", f.kind);
  if (f.actor) qs.set("actor_id", f.actor);
  if (f.channel) qs.set("channel_id", f.channel);
  const ms = RANGES.find(([id]) => id === f.range)?.[2];
  if (ms) qs.set("since", new Date(Date.now() - ms).toISOString());
  return qs;
}

/** Same rules as the server's search, for events arriving live. */
function matches(e: AuditEvent, f: Filters) {
  if (f.source && e.source !== f.source) return false;
  if (f.kind && e.kind !== f.kind) return false;
  if (f.actor && e.actor_id !== f.actor) return false;
  if (f.channel && e.channel_id !== f.channel) return false;
  const q = f.q.trim().toLowerCase();
  if (q)
    return [e.summary, e.action, e.actor_name, e.channel_name, e.target, e.actor_id].some((v) =>
      (v ?? "").toLowerCase().includes(q),
    );
  return true;
}

function EventRow({
  event,
  open,
  fresh,
  onToggle,
  onFilter,
  guildId,
  now,
}: {
  event: AuditEvent;
  open: boolean;
  fresh: boolean;
  onToggle: () => void;
  onFilter: (updates: Partial<Filters>) => void;
  guildId?: string;
  now: number;
}) {
  return (
    <li className={cx("transition-colors duration-1000", fresh ? "bg-gold/[0.07]" : open ? "bg-raised/40" : "")}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="grid w-full grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1 px-4 py-2.5 text-left transition hover:bg-raised/50 md:grid-cols-[92px_minmax(0,170px)_minmax(0,140px)_minmax(0,1fr)_16px] md:items-center md:px-5"
      >
        {/* member (mobile: first line) */}
        <span className="order-1 flex min-w-0 items-center gap-2 md:order-2">
          <Avatar url={event.actor_avatar_url} name={event.actor_name} size="h-5 w-5" />
          <span className="truncate font-medium">{event.actor_name ?? "Unknown"}</span>
        </span>
        <time
          dateTime={event.created_at}
          title={fullDateTime(event.created_at)}
          className="order-2 whitespace-nowrap text-right text-2xs tabular-nums text-faint md:order-1 md:text-left md:text-xs"
        >
          {shortDateTime(event.created_at, now)}
        </time>
        <span className="order-3 col-span-2 flex min-w-0 items-center gap-2 md:order-4 md:col-span-1">
          <KindBadge kind={event.kind} />
          <span className="truncate">{event.summary}</span>
        </span>
        <Where event={event} className="order-4 col-span-2 text-xs md:order-3 md:col-span-1 md:text-[13px]" />
        <ChevronRight
          className={cx("order-5 hidden h-4 w-4 text-faint transition md:block", open && "rotate-90 text-gold")}
        />
      </button>
      {open && (
        <div className="border-t border-line/60 bg-bg/40 px-4 py-3 md:px-5 md:pl-[124px]">
          <EventDetails event={event} guildId={guildId} />
          <div className="mt-2 flex flex-wrap gap-2">
            {event.actor_id && (
              <Button size="sm" variant="ghost" icon={<Users className="h-3.5 w-3.5" />} onClick={() => onFilter({ actor: event.actor_id! })}>
                Only {event.actor_name ?? "this member"}
              </Button>
            )}
            {event.channel_id && (
              <Button size="sm" variant="ghost" icon={<Filter className="h-3.5 w-3.5" />} onClick={() => onFilter({ channel: event.channel_id! })}>
                Only #{event.channel_name ?? "this channel"}
              </Button>
            )}
            <Button size="sm" variant="ghost" icon={<Filter className="h-3.5 w-3.5" />} onClick={() => onFilter({ kind: event.kind })}>
              Only {KINDS[event.kind]?.label.toLowerCase() ?? event.kind}s
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

export default function AuditLogPage() {
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => readFilters(params), [params]);
  const { subscribe, status } = useLive();
  const now = useNow();

  const [meta, setMeta] = useState<AuditMeta | null>(null);
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const [live, setLive] = useState(true);
  const [queued, setQueued] = useState<AuditEvent[]>([]);
  const [search, setSearch] = useState(filters.q);

  const setFilters = useCallback(
    (updates: Partial<Filters>) => {
      const next = new URLSearchParams(params);
      Object.entries(updates).forEach(([k, v]) => (v && !(k === "range" && v === "all") ? next.set(k, v) : next.delete(k)));
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  // Search box: update the URL (and so the results) once typing pauses.
  useEffect(() => setSearch(filters.q), [filters.q]);
  useEffect(() => {
    if (search === filters.q) return;
    const t = window.setTimeout(() => setFilters({ q: search.trim() }), 300);
    return () => window.clearTimeout(t);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadMeta = useCallback(() => {
    api.get<AuditMeta>("/api/audit/meta").then(setMeta).catch(() => undefined);
  }, []);
  useEffect(loadMeta, [loadMeta]);

  // (Re)load the first page whenever the filters change.
  const requestId = useRef(0);
  useEffect(() => {
    const id = ++requestId.current;
    setEvents(null);
    setError(null);
    setQueued([]);
    setExpanded(new Set());
    const qs = toQuery(filters);
    qs.set("limit", String(PAGE_SIZE));
    api
      .get<AuditPage>(`/api/audit?${qs}`)
      .then((page) => {
        if (id !== requestId.current) return;
        setEvents(page.events);
        setHasMore(page.has_more);
      })
      .catch((e) => id === requestId.current && setError(e instanceof ApiError ? e.message : "Couldn't load the audit log"));
  }, [filters]);

  const loadMore = () => {
    if (!events?.length) return;
    setLoadingMore(true);
    const qs = toQuery(filters);
    qs.set("limit", String(PAGE_SIZE));
    qs.set("before_id", String(events[events.length - 1].id));
    api
      .get<AuditPage>(`/api/audit?${qs}`)
      .then((page) => {
        setEvents((current) => [...(current ?? []), ...page.events]);
        setHasMore(page.has_more);
      })
      .catch(() => setError("Couldn't load older entries"))
      .finally(() => setLoadingMore(false));
  };

  // Live feed: new events matching the filters appear at the top (or wait
  // in a queue while paused).
  const liveRef = useRef(live);
  liveRef.current = live;
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  useEffect(
    () =>
      subscribe((message) => {
        if (message.type !== "audit.event") return;
        const event = message.event as AuditEvent;
        setMeta((m) =>
          m && {
            ...m,
            stats: {
              ...m.stats,
              events_24h: m.stats.events_24h + 1,
              events_7d: m.stats.events_7d + 1,
              dashboard_7d: m.stats.dashboard_7d + (event.source === "dashboard" ? 1 : 0),
              total: m.stats.total + 1,
            },
          },
        );
        if (!matches(event, filtersRef.current)) return;
        if (!liveRef.current) {
          setQueued((q) => [event, ...q]);
          return;
        }
        setEvents((current) => (current && !current.some((e) => e.id === event.id) ? [event, ...current] : current));
        setFresh((s) => new Set(s).add(event.id));
        window.setTimeout(() => setFresh((s) => {
          const next = new Set(s);
          next.delete(event.id);
          return next;
        }), 2500);
      }),
    [subscribe],
  );

  const showQueued = () => {
    setEvents((current) => {
      const known = new Set((current ?? []).map((e) => e.id));
      return [...queued.filter((e) => !known.has(e.id)), ...(current ?? [])];
    });
    setQueued([]);
  };

  const toggleLive = () => {
    if (!live) showQueued();
    setLive(!live);
  };

  const toggle = (id: number) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const activeFilters = Boolean(filters.q || filters.source || filters.kind || filters.actor || filters.channel || filters.range !== "all");
  const exportHref = `/api/audit/export?${toQuery(filters)}`;
  const stats = meta?.stats;

  const actorOptions = useMemo(() => {
    const list = meta?.actors ?? [];
    // Keep a member chosen from a row selectable even if not in the top list.
    if (filters.actor && !list.some((a) => a.id === filters.actor)) {
      const fromEvents = events?.find((e) => e.actor_id === filters.actor);
      return [{ id: filters.actor, name: fromEvents?.actor_name ?? "Selected member", count: 0, avatar_url: null }, ...list];
    }
    return list;
  }, [meta, filters.actor, events]);

  const channelOptions = useMemo(() => {
    const list = meta?.channels ?? [];
    if (filters.channel && !list.some((c) => c.id === filters.channel)) {
      const fromEvents = events?.find((e) => e.channel_id === filters.channel);
      return [{ id: filters.channel, name: fromEvents?.channel_name ?? "Selected channel", count: 0 }, ...list];
    }
    return list;
  }, [meta, filters.channel, events]);

  return (
    <>
      <SectionHeader
        eyebrow="System"
        title="Audit log"
        description="Who did what with the bot, in Discord and on this dashboard. What members type into forms is never recorded."
        actions={
          <>
            <Button
              size="sm"
              variant={live ? "primary" : "outline"}
              onClick={toggleLive}
              icon={live ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              title={live ? "New entries appear as they happen. Click to pause." : "Paused. Click to show new entries as they happen."}
            >
              {live ? "Live" : "Paused"}
            </Button>
            <a
              href={exportHref}
              download
              className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-line bg-raised/60 px-2.5 text-xs font-medium transition hover:border-faint hover:bg-raised"
              title="Download the entries matching these filters as a spreadsheet (CSV, up to 10,000 rows)"
            >
              <Download className="h-3.5 w-3.5" /> Export
            </a>
          </>
        }
      />

      {meta && !meta.discord_capture_enabled && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-warn">Discord actions aren't being recorded.</span>{" "}
            <span className="text-muted">The Audit log feature is off, so only dashboard actions show up here.</span>
          </span>
          <Link to="/settings?group=features#FEATURE_AUDIT_LOG_ENABLED" className="text-xs text-gold hover:underline">
            Turn it on
          </Link>
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats ? (
          <>
            <StatTile value={stats.events_24h} label="Actions, last 24h" icon={<Activity className="h-3 w-3" />} tone={stats.events_24h ? "gold" : undefined} />
            <StatTile value={stats.events_7d} label="Actions, last 7 days" icon={<ScrollText className="h-3 w-3" />} />
            <StatTile value={stats.members_7d} label="Active members, 7 days" icon={<Users className="h-3 w-3" />} />
            <StatTile value={stats.dashboard_7d} label="Dashboard actions, 7 days" icon={<MonitorCog className="h-3 w-3" />} />
          </>
        ) : (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[76px]" />)
        )}
      </div>

      <section className="card">
        <div className="space-y-2.5 border-b border-line p-3 md:px-5 md:py-4">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[200px] flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
              <input
                className="input pl-9"
                placeholder="Search actions, members, channels…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                aria-label="Search the audit log"
              />
            </div>
            <Segmented label="Time range" value={filters.range} options={RANGES} onChange={(range) => setFilters({ range })} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Segmented label="Where" value={filters.source} options={SOURCES} onChange={(source) => setFilters({ source })} />
            <select
              className="input h-[34px] w-auto min-w-[140px] flex-1 py-1 sm:flex-none"
              value={filters.kind}
              onChange={(e) => setFilters({ kind: e.target.value })}
              aria-label="Type of action"
            >
              <option value="">All action types</option>
              {(Object.keys(KINDS) as AuditKind[]).map((kind) => (
                <option key={kind} value={kind}>
                  {KINDS[kind].label}
                  {meta ? ` (${meta.kinds.find((k) => k.kind === kind)?.count ?? 0})` : ""}
                </option>
              ))}
            </select>
            <select
              className="input h-[34px] w-auto min-w-[140px] flex-1 py-1 sm:flex-none"
              value={filters.actor}
              onChange={(e) => setFilters({ actor: e.target.value })}
              aria-label="Member"
            >
              <option value="">All members</option>
              {actorOptions.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name ?? a.id}
                  {a.count ? ` (${a.count})` : ""}
                </option>
              ))}
            </select>
            <select
              className="input h-[34px] w-auto min-w-[140px] flex-1 py-1 sm:flex-none"
              value={filters.channel}
              onChange={(e) => setFilters({ channel: e.target.value })}
              aria-label="Channel"
            >
              <option value="">All channels</option>
              {channelOptions.map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.name ?? c.id}
                  {c.count ? ` (${c.count})` : ""}
                </option>
              ))}
            </select>
            {activeFilters && (
              <Button size="sm" variant="ghost" icon={<X className="h-3.5 w-3.5" />} onClick={() => setParams(new URLSearchParams(), { replace: true })}>
                Clear filters
              </Button>
            )}
          </div>
        </div>

        {queued.length > 0 && (
          <button
            type="button"
            onClick={showQueued}
            className="flex w-full items-center justify-center gap-1.5 border-b border-line bg-gold/[0.06] py-2 text-xs font-medium text-gold transition hover:bg-gold/10"
          >
            <ArrowUp className="h-3.5 w-3.5" /> Show {queued.length} new entr{queued.length === 1 ? "y" : "ies"}
          </button>
        )}

        <div className="hidden grid-cols-[92px_minmax(0,170px)_minmax(0,140px)_minmax(0,1fr)_16px] gap-x-3 border-b border-line px-5 py-2 md:grid">
          {["Time", "Member", "Where", "Action", ""].map((h) => (
            <div key={h} className="label">
              {h}
            </div>
          ))}
        </div>

        {error ? (
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load the audit log">
            {error}
          </EmptyState>
        ) : events === null ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : events.length === 0 ? (
          <EmptyState icon={<ScrollText className="h-6 w-6" />} title={activeFilters ? "Nothing matches these filters" : "Nothing recorded yet"}>
            {activeFilters
              ? "Try a longer time range or clear the filters."
              : "Actions show up here as soon as someone uses a command, button or form, or changes something on this dashboard."}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {events.map((event) => (
              <EventRow
                key={event.id}
                event={event}
                open={expanded.has(event.id)}
                fresh={fresh.has(event.id)}
                onToggle={() => toggle(event.id)}
                onFilter={setFilters}
                guildId={status?.guild?.id}
                now={now}
              />
            ))}
          </ul>
        )}

        {events && events.length > 0 && (
          <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3 text-2xs text-faint md:px-5">
            <span>
              Showing {events.length.toLocaleString()} entr{events.length === 1 ? "y" : "ies"}
              {stats && stats.retention_days > 0 && ` · entries older than ${stats.retention_days} days are removed automatically`}
            </span>
            {hasMore ? (
              <Button size="sm" onClick={loadMore} loading={loadingMore} icon={<ChevronDown className="h-3.5 w-3.5" />}>
                Load older
              </Button>
            ) : (
              <span>That's everything{activeFilters ? " matching these filters" : ""}.</span>
            )}
          </footer>
        )}
      </section>
    </>
  );
}
