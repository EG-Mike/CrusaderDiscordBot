// Command palette (Ctrl+K / Cmd+K): jump to any page, setting, announcement or
// bot message, or run a common action, from the keyboard. Only offers what
// the person may do; the server still checks every action.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowRight,
  BellRing,
  CornerDownLeft,
  FileText,
  LogOut,
  Megaphone,
  MessageSquareText,
  Plus,
  RotateCcw,
  Search,
  Settings2,
  Smile,
  type LucideIcon,
} from "lucide-react";
import { ApiError, api, type Announcement, type AnnouncementList, type BotMessage, type Setting } from "../lib/api";
import { useLive } from "../lib/live";
import { useMe } from "../lib/me";
import { NAV } from "../lib/nav";
import { canRestart, useRestart } from "./Restart";
import { useToast } from "./Toast";
import { Button, Dialog, cx } from "./ui";

type Group = "Actions" | "Pages" | "Settings" | "Announcements" | "Bot messages";
const GROUP_ORDER: Group[] = ["Actions", "Pages", "Announcements", "Bot messages", "Settings"];

interface Command {
  id: string;
  group: Group;
  label: string;
  hint?: string;
  keywords?: string;
  icon: LucideIcon;
  run: () => void | Promise<void>;
}

/** Every query word must appear; earlier and word-start matches rank higher. */
function score(command: Command, words: string[]): number {
  if (!words.length) return 1;
  const label = command.label.toLowerCase();
  const haystack = `${label} ${(command.hint ?? "").toLowerCase()} ${(command.keywords ?? "").toLowerCase()}`;
  let total = 0;
  for (const word of words) {
    const at = haystack.indexOf(word);
    if (at < 0) return 0;
    total += label.startsWith(word) ? 6 : label.includes(` ${word}`) ? 4 : at < label.length ? 3 : 1;
  }
  return total;
}

export function useCommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return { open, setOpen };
}

export function PaletteTrigger({ onOpen }: { onOpen: () => void }) {
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
  return (
    <>
      <button
        type="button"
        onClick={onOpen}
        className="relative hidden h-9 w-full max-w-md items-center rounded-lg border border-line bg-surface pl-9 pr-14 text-left text-[13px] text-faint transition hover:border-faint md:flex"
      >
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2" />
        Search pages, settings and actions…
        <kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-line bg-raised px-1.5 py-px text-2xs">{mac ? "⌘ K" : "Ctrl K"}</kbd>
      </button>
      <button type="button" onClick={onOpen} className="rounded-lg p-1.5 text-muted hover:bg-raised md:hidden" aria-label="Search">
        <Search className="h-5 w-5" />
      </button>
    </>
  );
}

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const me = useMe();
  const toast = useToast();
  const { status } = useLive();
  const { restart, overlay } = useRestart();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [settings, setSettings] = useState<Setting[]>([]);
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [messages, setMessages] = useState<BotMessage[]>([]);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // Fresh data each time it opens (cheap, and never stale).
  useEffect(() => {
    if (!open) return;
    setQ("");
    setActive(0);
    window.setTimeout(() => inputRef.current?.focus(), 0);
    api.get<{ settings: Setting[] }>("/api/settings").then((d) => setSettings(d.settings)).catch(() => undefined);
    api.get<AnnouncementList>("/api/announcements").then((d) => setAnnouncements(d.announcements)).catch(() => undefined);
    api.get<BotMessage[]>("/api/bot-messages").then(setMessages).catch(() => undefined);
  }, [open]);

  const go = useCallback(
    (path: string) => () => {
      navigate(path);
    },
    [navigate],
  );

  const commands: Command[] = useMemo(() => {
    const list: Command[] = [
      {
        id: "new-announcement",
        group: "Actions",
        label: "New announcement",
        keywords: "write post announce draft",
        icon: Plus,
        run: async () => {
          try {
            const created = await api.post<Announcement>("/api/announcements", {});
            navigate(`/announcements/${created.id}`);
          } catch (e) {
            toast("error", e instanceof ApiError ? e.message : "Couldn't start a new announcement");
          }
        },
      },
      {
        id: "test-notification",
        group: "Actions",
        label: "Send a test notification",
        keywords: "notify alert channel",
        icon: BellRing,
        run: async () => {
          try {
            const { channel } = await api.post<{ channel: string }>("/api/notifications/test");
            toast("ok", `Test notification posted in #${channel}`);
          } catch (e) {
            toast("error", e instanceof ApiError ? e.message : "Couldn't post the test notification");
          }
        },
      },
      { id: "add-emoji", group: "Actions", label: "Add an emoji", keywords: "icon item spell", icon: Smile, run: go("/emoji") },
      { id: "errors", group: "Actions", label: "Show recent errors", keywords: "system log console problems", icon: FileText, run: go("/system-log?view=errors&range=24h") },
    ];
    if (canRestart(me, status))
      list.push({ id: "restart", group: "Actions", label: "Restart the bot", keywords: "reboot reload apply settings", icon: RotateCcw, run: () => setConfirmRestart(true) });
    list.push({
      id: "logout",
      group: "Actions",
      label: "Log out",
      icon: LogOut,
      run: async () => {
        await api.post("/auth/logout").catch(() => undefined);
        window.location.href = "/login";
      },
    });
    for (const { group, items } of NAV)
      for (const item of items)
        list.push({
          id: `page:${item.to}`,
          group: "Pages",
          label: item.label,
          hint: item.soon ? "Coming soon" : group || undefined,
          icon: item.icon,
          run: go(item.to),
        });
    for (const a of announcements)
      list.push({
        id: `ann:${a.id}`,
        group: "Announcements",
        label: a.title || "Untitled announcement",
        hint: a.status === "published" ? `Published${a.channel_name ? ` in #${a.channel_name}` : ""}` : "Draft",
        keywords: a.body.slice(0, 300),
        icon: Megaphone,
        run: go(`/announcements/${a.id}`),
      });
    for (const m of messages)
      list.push({ id: `msg:${m.key}`, group: "Bot messages", label: m.label, hint: m.where, icon: MessageSquareText, run: go(`/bot-messages?key=${m.key}`) });
    for (const s of settings)
      list.push({
        id: `set:${s.key}`,
        group: "Settings",
        label: s.label,
        hint: s.key,
        keywords: s.help.slice(0, 200),
        icon: Settings2,
        run: go(`/settings?group=${s.group}#${s.key}`),
      });
    return list;
  }, [announcements, messages, settings, me, status, navigate, toast, go]);

  const results = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const scored = commands.map((c) => ({ c, s: score(c, words) })).filter((x) => x.s > 0);
    if (!words.length) {
      // Without a query: actions and pages only, settings would be a wall.
      return GROUP_ORDER.filter((g) => g === "Actions" || g === "Pages").flatMap((g) => scored.filter((x) => x.c.group === g).map((x) => x.c));
    }
    // Groups in order of their best match, so Enter picks the best result.
    const perGroup = GROUP_ORDER.map((g) => scored.filter((x) => x.c.group === g).sort((a, b) => b.s - a.s).slice(0, 8)).filter(
      (items) => items.length,
    );
    perGroup.sort((a, b) => b[0].s - a[0].s);
    return perGroup.flat().map((x) => x.c);
  }, [commands, q]);

  useEffect(() => setActive(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const runCommand = async (command: Command | undefined) => {
    if (!command) return;
    onClose();
    await command.run();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runCommand(results[active]);
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  let index = -1;
  const sections: ReactNode[] = [];
  for (const group of results.map((c) => c.group).filter((g, i, all) => all.indexOf(g) === i)) {
    const items = results.filter((c) => c.group === group);
    if (!items.length) continue;
    sections.push(
      <li key={group} role="presentation" className="px-3 pb-1 pt-3 text-2xs font-semibold uppercase tracking-label text-faint">
        {group}
      </li>,
    );
    for (const c of items) {
      index += 1;
      const i = index;
      sections.push(
        <li
          key={c.id}
          role="option"
          aria-selected={i === active}
          data-index={i}
          id={`cmd-${i}`}
          onMouseMove={() => setActive(i)}
          onClick={() => runCommand(c)}
          className={cx("mx-1.5 flex cursor-pointer items-center gap-3 rounded-lg px-2.5 py-2", i === active ? "bg-gold/10 text-fg" : "text-muted")}
        >
          <c.icon className={cx("h-4 w-4 shrink-0", i === active ? "text-gold" : "text-faint")} />
          <span className="min-w-0 flex-1 truncate">{c.label}</span>
          {c.hint && <span className="hidden max-w-[45%] truncate text-2xs text-faint sm:inline">{c.hint}</span>}
          {i === active && <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-faint" />}
        </li>,
      );
    }
  }

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center p-3 pt-[12vh]" role="dialog" aria-modal="true" aria-label="Command palette">
          <div className="absolute inset-0 bg-black/60" onClick={onClose} />
          <div className="card relative flex max-h-[70vh] w-full max-w-xl flex-col overflow-hidden shadow-2xl shadow-black/60">
            <div className="flex items-center gap-2.5 border-b border-line px-4">
              <Search className="h-4 w-4 shrink-0 text-faint" />
              <input
                ref={inputRef}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Search pages, settings, announcements, actions…"
                className="h-12 w-full bg-transparent text-[14px] text-fg outline-none placeholder:text-faint"
                aria-label="Search"
                aria-controls="palette-results"
                aria-activedescendant={results[active] ? `cmd-${active}` : undefined}
              />
              <kbd className="shrink-0 rounded border border-line bg-raised px-1.5 py-px text-2xs text-faint">Esc</kbd>
            </div>
            <ul id="palette-results" ref={listRef} role="listbox" className="scrollbar-thin flex-1 overflow-y-auto pb-2">
              {sections.length ? sections : <li className="px-4 py-8 text-center text-muted">Nothing matches “{q}”.</li>}
            </ul>
            <div className="flex items-center gap-3 border-t border-line px-4 py-2 text-2xs text-faint">
              <span>↑↓ to move</span>
              <span>Enter to open</span>
              <span className="ml-auto inline-flex items-center gap-1">
                <ArrowRight className="h-3 w-3" /> Ctrl/⌘ K anywhere
              </span>
            </div>
          </div>
        </div>
      )}
      <Dialog
        open={confirmRestart}
        onClose={() => setConfirmRestart(false)}
        title="Restart the bot?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setConfirmRestart(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirmRestart(false);
                restart();
              }}
            >
              Restart
            </Button>
          </>
        }
      >
        <p className="text-muted">The bot goes offline for about 10 to 30 seconds and applies any settings waiting for a restart.</p>
      </Dialog>
      {overlay}
    </>
  );
}
