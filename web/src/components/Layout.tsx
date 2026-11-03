import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  BarChart3,
  CalendarCheck,
  ChevronDown,
  ClipboardList,
  FileText,
  LayoutDashboard,
  LogOut,
  Megaphone,
  Menu,
  ScrollText,
  Search,
  Settings,
  Swords,
  Trophy,
  Vote,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { api, type Me } from "../lib/api";
import { formatDuration, useLive } from "../lib/live";
import { Badge, cx } from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  soon?: boolean;
}

export const NAV: { group: string; items: NavItem[] }[] = [
  { group: "", items: [{ to: "/", label: "Overview", icon: LayoutDashboard }] },
  {
    group: "Communication",
    items: [
      { to: "/announcements", label: "Announcements", icon: Megaphone, soon: true },
      { to: "/polls", label: "Polls", icon: Vote, soon: true },
    ],
  },
  {
    group: "Members",
    items: [
      { to: "/applications", label: "Applications", icon: ClipboardList, soon: true },
      { to: "/attendance", label: "Attendance", icon: CalendarCheck, soon: true },
    ],
  },
  {
    group: "Raids",
    items: [
      { to: "/raid-summaries", label: "Raid summaries", icon: Swords, soon: true },
      { to: "/raid-logs", label: "Raid logs", icon: FileText, soon: true },
      { to: "/records", label: "Records", icon: Trophy, soon: true },
      { to: "/tier-recap", label: "Tier recap", icon: BarChart3, soon: true },
    ],
  },
  {
    group: "System",
    items: [
      { to: "/audit-log", label: "Audit log", icon: ScrollText, soon: true },
      { to: "/settings", label: "Settings", icon: Settings },
      { to: "/maintenance", label: "Maintenance", icon: Wrench, soon: true },
    ],
  },
];

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { status } = useLive();
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2.5 border-b border-line px-4 py-3.5">
        {status?.guild?.icon_url ? (
          <img src={status.guild.icon_url} alt="" className="h-7 w-7 rounded-full ring-1 ring-line" />
        ) : (
          <div className="h-7 w-7 rounded-full bg-raised ring-1 ring-line" />
        )}
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold">{status?.guild?.name ?? "Your server"}</div>
          <div className="text-2xs text-faint">{status?.guild ? `${status.guild.member_count} members` : " "}</div>
        </div>
      </div>

      <nav className="scrollbar-thin flex-1 overflow-y-auto px-2.5 py-3">
        {NAV.map(({ group, items }) => (
          <div key={group || "root"} className="mb-3">
            {group && <div className="label px-2 pb-1.5 pt-2">{group}</div>}
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cx(
                    "group relative flex items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-[13px] transition",
                    isActive ? "bg-gold/10 text-fg" : "text-muted hover:bg-raised hover:text-fg",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && <span className="absolute -left-2.5 top-1.5 bottom-1.5 w-[3px] rounded-r bg-gold" />}
                    <item.icon className={cx("h-4 w-4", isActive ? "text-gold" : "text-faint group-hover:text-muted")} />
                    <span className="flex-1">{item.label}</span>
                    {item.soon && <Badge className="!px-1 !text-[9px]">Soon</Badge>}
                  </>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="border-t border-line px-4 py-3 text-2xs text-faint">
        <div className="flex items-center justify-between">
          <span>Version {status?.version ?? "…"}</span>
          {status && <span>Up {formatDuration(status.uptime_seconds)}</span>}
        </div>
      </div>
    </div>
  );
}

function BotStatusPill() {
  const { status, connected } = useLive();
  const online = connected && status?.online;
  return (
    <div
      className={cx(
        "hidden items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs sm:flex",
        online ? "border-ok/25 bg-ok/5 text-ok" : "border-crimson/30 bg-crimson/5 text-crimson",
      )}
      title={connected ? "Live connection to the bot" : "Reconnecting to the bot…"}
    >
      <span className="relative flex h-2 w-2">
        {online && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ok opacity-50" />}
        <span className={cx("relative inline-flex h-2 w-2 rounded-full", online ? "bg-ok" : "bg-crimson")} />
      </span>
      {online ? (
        <span>
          Bot online{status?.latency_ms != null && <span className="text-ok/70"> · {status.latency_ms} ms</span>}
        </span>
      ) : (
        <span>{connected ? "Bot offline" : "Reconnecting…"}</span>
      )}
    </div>
  );
}

function UserMenu({ me }: { me: Me }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const logout = async () => {
    await api.post("/auth/logout").catch(() => undefined);
    window.location.href = "/login";
  };

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-lg px-1.5 py-1 transition hover:bg-raised"
      >
        <img src={me.avatar_url} alt="" className="h-7 w-7 rounded-full ring-1 ring-line" />
        <span className="hidden text-[13px] font-medium md:inline">{me.username}</span>
        <ChevronDown className="h-3.5 w-3.5 text-faint" />
      </button>
      {open && (
        <div className="card absolute right-0 top-11 z-30 w-56 p-1.5 shadow-2xl shadow-black/50">
          <div className="px-2.5 py-2">
            <div className="font-medium">{me.username}</div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {me.levels.map((level) => (
                <Badge key={level} tone={level === "admin" ? "gold" : "neutral"}>
                  {level}
                </Badge>
              ))}
            </div>
          </div>
          <div className="my-1 h-px bg-line" />
          <button
            onClick={logout}
            className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-muted transition hover:bg-raised hover:text-fg"
          >
            <LogOut className="h-4 w-4" /> Log out
          </button>
        </div>
      )}
    </div>
  );
}

function SearchBox() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <form
      className="relative hidden w-full max-w-md md:block"
      onSubmit={(e) => {
        e.preventDefault();
        if (q.trim()) navigate(`/settings?q=${encodeURIComponent(q.trim())}`);
      }}
    >
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
      <input
        ref={inputRef}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search settings…"
        className="h-9 w-full rounded-lg border border-line bg-surface pl-9 pr-14 text-[13px] outline-none transition placeholder:text-faint focus:border-gold/50"
      />
      <kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-line bg-raised px-1.5 py-px text-2xs text-faint">
        Ctrl K
      </kbd>
    </form>
  );
}

function Breadcrumb() {
  const { pathname } = useLocation();
  const { status } = useLive();
  const item = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.group }))).find((i) =>
    i.to === "/" ? pathname === "/" : pathname.startsWith(i.to),
  );
  return (
    <div className="mb-5 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-label">
      <Link to="/" className="text-faint transition hover:text-muted">
        {status?.guild?.name ?? "Dashboard"}
      </Link>
      <span className="text-faint">/</span>
      <span className="text-gold">{item?.label ?? "Not found"}</span>
    </div>
  );
}

export function Layout({ me, children }: { me: Me; children: ReactNode }) {
  const { status } = useLive();
  const [mobileNav, setMobileNav] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setMobileNav(false), [pathname]);

  return (
    <div className="app-glow flex h-full flex-col">
      <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-bg/85 px-3 backdrop-blur md:px-4">
        <button className="rounded-lg p-1.5 text-muted hover:bg-raised lg:hidden" onClick={() => setMobileNav(true)} aria-label="Open menu">
          <Menu className="h-5 w-5" />
        </button>
        <Link to="/" className="flex items-center gap-2.5 lg:w-[218px]">
          <img src="/crest.svg" alt="" className="h-6 w-6" />
          <span className="text-[13px] font-bold tracking-[0.2em]">CRUSADER</span>
          {status?.environment ? (
            <Badge tone="warn">{status.environment}</Badge>
          ) : (
            <Badge tone="gold">Beta</Badge>
          )}
        </Link>
        <div className="flex flex-1 justify-center">
          <SearchBox />
        </div>
        <BotStatusPill />
        <UserMenu me={me} />
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-[250px] shrink-0 border-r border-line bg-bg/60 lg:block">
          <Sidebar />
        </aside>

        {mobileNav && (
          <div className="fixed inset-0 z-40 lg:hidden">
            <div className="absolute inset-0 bg-black/60" onClick={() => setMobileNav(false)} />
            <aside className="absolute inset-y-0 left-0 w-[270px] border-r border-line bg-bg">
              <button className="absolute right-3 top-3.5 rounded-lg p-1 text-muted hover:bg-raised" onClick={() => setMobileNav(false)} aria-label="Close menu">
                <X className="h-4 w-4" />
              </button>
              <Sidebar onNavigate={() => setMobileNav(false)} />
            </aside>
          </div>
        )}

        <main className="scrollbar-thin min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1200px] px-4 py-5 md:px-8 md:py-6">
            <Breadcrumb />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
