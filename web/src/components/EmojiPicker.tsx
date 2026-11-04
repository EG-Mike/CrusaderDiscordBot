// Emoji picker for the text editors: the bot's own emoji (searchable by the
// items/spells that use each icon), the server's custom emoji, and the
// standard set. Picking inserts what Discord expects: <:name:id> for custom
// emoji, the character itself for standard ones.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, Search, Smile } from "lucide-react";
import type { EmojiOverview } from "../lib/api";
import { STANDARD_GROUPS, loadCustomEmoji, loadStandardEmoji, type StandardEmojiSet } from "../lib/emoji";
import { cx } from "./ui";

type Tab = "bot" | "server" | "standard";

interface Cell {
  key: string;
  title: string;
  insert: string;
  url?: string;
  char?: string;
}

const MAX_CELLS = 400;

export function EmojiPicker({ onPick, disabled }: { onPick: (text: string) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("bot");
  const [q, setQ] = useState("");
  const [custom, setCustom] = useState<EmojiOverview | null>(null);
  const [standard, setStandard] = useState<StandardEmojiSet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [group, setGroup] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    loadCustomEmoji()
      .then((data) => {
        setCustom(data);
        if (!data.library.length && !data.other.length && data.server.length) setTab((t) => (t === "bot" ? "server" : t));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load emoji"));
    loadStandardEmoji().then(setStandard).catch(() => undefined);
    searchRef.current?.focus();
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const needle = q.trim().toLowerCase().replace(/^:|:$/g, "");
  const cells: Cell[] = useMemo(() => {
    if (tab === "bot") {
      const library = (custom?.library ?? [])
        .filter((e) => !needle || e.name.includes(needle) || e.aliases.some((a) => a.name.toLowerCase().includes(needle)))
        .map((e) => ({
          key: e.emoji_id,
          url: e.url,
          insert: e.text,
          title: `:${e.name}:${e.aliases.length ? ` - ${e.aliases.map((a) => a.name).join(", ")}` : ""}`,
        }));
      const other = (custom?.other ?? [])
        .filter((e) => !needle || e.name.toLowerCase().includes(needle))
        .map((e) => ({ key: e.emoji_id, url: e.url, insert: e.text, title: `:${e.name}:` }));
      return [...library, ...other];
    }
    if (tab === "server")
      return (custom?.server ?? [])
        .filter((e) => e.available !== false && (!needle || e.name.toLowerCase().includes(needle)))
        .map((e) => ({ key: e.emoji_id, url: e.url, insert: e.text, title: `:${e.name}:` }));
    return (standard?.list ?? [])
      .filter((e) => (needle ? e.label.toLowerCase().includes(needle) || e.shortcodes.some((s) => s.includes(needle)) : e.group === group))
      .slice(0, MAX_CELLS)
      .map((e) => ({ key: e.char, char: e.char, insert: e.char, title: `${e.shortcodes[0] ? `:${e.shortcodes[0]}: ` : ""}${e.label}` }));
  }, [tab, custom, standard, needle, group]);

  const pick = (cell: Cell) => {
    onPick(cell.insert);
    setOpen(false);
    setQ("");
  };

  const loading = tab === "standard" ? !standard : !custom && !error;

  return (
    <div className="relative shrink-0" ref={ref}>
      <button
        type="button"
        title="Emoji"
        aria-label="Insert emoji"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={cx(
          "flex h-7 w-7 items-center justify-center rounded-md transition hover:bg-raised hover:text-fg disabled:opacity-40",
          open ? "bg-raised text-gold" : "text-muted",
        )}
      >
        <Smile className="h-3.5 w-3.5" />
      </button>
      {open && (
        <div
          className="card fixed inset-x-2 bottom-2 z-40 flex max-h-[70vh] flex-col shadow-2xl shadow-black/60 sm:absolute sm:inset-x-auto sm:bottom-auto sm:left-0 sm:top-9 sm:w-[352px]"
          role="dialog"
          aria-label="Emoji"
        >
          <div className="flex gap-1 border-b border-line p-1.5">
            {(
              [
                ["bot", "Bot emoji"],
                ["server", "Server"],
                ["standard", "Standard"],
              ] as [Tab, string][]
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                onClick={() => setTab(id)}
                className={cx("flex-1 rounded-md px-2 py-1 text-xs font-medium transition", tab === id ? "bg-gold/15 text-gold" : "text-muted hover:text-fg")}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="relative border-b border-line p-1.5">
            <Search className="pointer-events-none absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
            <input
              ref={searchRef}
              className="input h-8 py-1 pl-8 text-xs"
              placeholder={tab === "bot" ? "Search emoji, items or spells…" : "Search emoji…"}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && cells[0]) {
                  e.preventDefault();
                  pick(cells[0]);
                }
              }}
              aria-label="Search emoji"
            />
          </div>
          {tab === "standard" && !needle && (
            <div className="scrollbar-thin flex gap-0.5 overflow-x-auto border-b border-line px-1.5 py-1">
              {STANDARD_GROUPS.map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setGroup(id)}
                  className={cx("shrink-0 rounded px-1.5 py-0.5 text-2xs transition", group === id ? "bg-raised text-fg" : "text-faint hover:text-muted")}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <div className="scrollbar-thin min-h-[120px] flex-1 overflow-y-auto p-1.5">
            {error && tab !== "standard" ? (
              <p className="p-3 text-center text-xs text-crimson">{error}</p>
            ) : loading ? (
              <div className="flex justify-center p-6 text-faint">
                <Loader2 className="h-4 w-4 animate-spin" />
              </div>
            ) : cells.length === 0 ? (
              <p className="p-4 text-center text-xs text-muted">
                {needle
                  ? "Nothing matches."
                  : tab === "bot"
                    ? "The bot has no emoji yet."
                    : tab === "server"
                      ? "This server has no custom emoji."
                      : "Nothing here."}
              </p>
            ) : (
              <div className="grid grid-cols-8 gap-0.5">
                {cells.map((cell) => (
                  <button
                    key={cell.key}
                    type="button"
                    title={cell.title}
                    aria-label={cell.title}
                    onClick={() => pick(cell)}
                    className="flex aspect-square items-center justify-center rounded-md text-[22px] leading-none transition hover:bg-raised"
                  >
                    {cell.url ? <img src={cell.url} alt="" loading="lazy" className="h-7 w-7 object-contain" /> : cell.char}
                  </button>
                ))}
              </div>
            )}
          </div>
          {tab === "bot" && (
            <div className="border-t border-line px-3 py-1.5 text-2xs text-faint">
              Item or spell missing?{" "}
              <Link to="/emoji" className="text-gold hover:underline">
                Add it on the Emoji page
              </Link>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
