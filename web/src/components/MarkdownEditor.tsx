// Text area for Discord-markdown with a formatting toolbar, a mention picker
// (roles and channels by name, never raw IDs), optional {placeholder} chips
// and a character counter. Shared by the announcement builder and the bot
// messages page.
import { useRef, type ReactNode } from "react";
import {
  Bold,
  Code,
  Heading2,
  Italic,
  Link2,
  List,
  Minus,
  Quote,
  Strikethrough,
  Type,
  Underline,
  type LucideIcon,
} from "lucide-react";
import type { Channel, Role } from "../lib/api";
import { EmojiPicker } from "./EmojiPicker";
import { cx } from "./ui";

type Edit = (text: string, start: number, end: number) => { text: string; select: [number, number] };

const wrap =
  (before: string, after = before, placeholder = "text"): Edit =>
  (text, start, end) => {
    const inner = text.slice(start, end) || placeholder;
    const next = text.slice(0, start) + before + inner + after + text.slice(end);
    return { text: next, select: [start + before.length, start + before.length + inner.length] };
  };

/** Adds a prefix to every selected line (or the current line). */
const prefixLines =
  (prefix: string): Edit =>
  (text, start, end) => {
    const lineStart = text.lastIndexOf("\n", start - 1) + 1;
    const block = text.slice(lineStart, end);
    const changed = block
      .split("\n")
      .map((line) => prefix + line)
      .join("\n");
    const next = text.slice(0, lineStart) + changed + text.slice(end);
    return { text: next, select: [lineStart + prefix.length, lineStart + changed.length] };
  };

const insert =
  (snippet: string): Edit =>
  (text, start, end) => {
    const next = text.slice(0, start) + snippet + text.slice(end);
    return { text: next, select: [start + snippet.length, start + snippet.length] };
  };

const TOOLS: { icon: LucideIcon; label: string; edit: Edit; sectionsOnly?: boolean }[] = [
  { icon: Bold, label: "Bold", edit: wrap("**") },
  { icon: Italic, label: "Italic", edit: wrap("*") },
  { icon: Underline, label: "Underline", edit: wrap("__") },
  { icon: Strikethrough, label: "Strikethrough", edit: wrap("~~") },
  { icon: Heading2, label: "Heading", edit: prefixLines("## ") },
  { icon: Type, label: "Small grey text", edit: prefixLines("-# ") },
  { icon: Quote, label: "Quote", edit: prefixLines("> ") },
  { icon: List, label: "Bulleted list", edit: prefixLines("- ") },
  { icon: Code, label: "Code", edit: wrap("`") },
  { icon: Link2, label: "Link", edit: wrap("[", "](https://)", "link text") },
  { icon: Minus, label: "New section (a divider line)", edit: insert("\n---\n"), sectionsOnly: true },
];

export function MarkdownEditor({
  value,
  onChange,
  limit,
  rows = 14,
  roles,
  channels,
  sections,
  placeholders,
  disabled,
  placeholder,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  limit?: number;
  rows?: number;
  roles?: Role[];
  channels?: Channel[];
  /** Offer the "new section" divider (announcements). */
  sections?: boolean;
  placeholders?: { name: string; description: string }[];
  disabled?: boolean;
  placeholder?: string;
  label: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  const apply = (edit: Edit) => {
    const el = ref.current;
    if (!el || disabled) return;
    const result = edit(value, el.selectionStart, el.selectionEnd);
    onChange(result.text);
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(...result.select);
    });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const shortcut = { b: wrap("**"), i: wrap("*"), u: wrap("__") }[e.key.toLowerCase()];
    if (shortcut) {
      e.preventDefault();
      apply(shortcut);
    }
  };

  const mentionables = [
    ...(roles ?? []).filter((r) => !r.managed).map((r) => ({ value: `<@&${r.id}>`, text: `@${r.name}`, group: "Roles" })),
    ...(channels ?? []).filter((c) => c.type !== "voice" && c.type !== "stage_voice").map((c) => ({ value: `<#${c.id}>`, text: `#${c.name}`, group: "Channels" })),
  ];

  const over = limit !== undefined && value.length > limit;

  return (
    <div className={cx("overflow-hidden rounded-lg border bg-bg transition focus-within:border-gold/60", over ? "border-crimson/60" : "border-line")}>
      <div className="flex items-center gap-0.5 border-b border-line px-1.5 py-1" role="toolbar" aria-label="Formatting">
        {/* The picker sits outside the scrolling part so its popup isn't clipped. */}
        <EmojiPicker onPick={(text) => apply(insert(text))} disabled={disabled} />
        <div className="scrollbar-thin flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
        {TOOLS.filter((t) => !t.sectionsOnly || sections).map((tool) => (
          <ToolButton key={tool.label} label={tool.label} onClick={() => apply(tool.edit)} disabled={disabled}>
            <tool.icon className="h-3.5 w-3.5" />
          </ToolButton>
        ))}
        {mentionables.length > 0 && (
          <select
            className="ml-1 h-7 shrink-0 rounded-md border border-line bg-surface px-1.5 text-xs text-muted outline-none hover:text-fg"
            value=""
            disabled={disabled}
            onChange={(e) => e.target.value && apply(insert(e.target.value))}
            aria-label="Insert a mention"
          >
            <option value="">Mention…</option>
            {["Roles", "Channels"].map((group) => (
              <optgroup key={group} label={group}>
                {mentionables
                  .filter((m) => m.group === group)
                  .map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.text}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
        )}
        </div>
      </div>
      <textarea
        ref={ref}
        aria-label={label}
        className="block w-full resize-y bg-transparent px-3 py-2.5 font-mono text-[12.5px] leading-relaxed text-fg outline-none placeholder:text-faint disabled:opacity-60"
        rows={rows}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        spellCheck
      />
      {(placeholders?.length || limit !== undefined) && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-line px-2.5 py-1.5">
          {placeholders?.map((p) => (
            <button
              key={p.name}
              type="button"
              disabled={disabled}
              onClick={() => apply(insert(`{${p.name}}`))}
              title={`Insert: ${p.description}`}
              className="rounded border border-gold/30 bg-gold/10 px-1.5 py-px font-mono text-2xs text-gold transition hover:bg-gold/20"
            >
              {`{${p.name}}`}
            </button>
          ))}
          {limit !== undefined && (
            <span className={cx("ml-auto text-2xs tabular-nums", over ? "font-semibold text-crimson" : "text-faint")}>
              {value.length.toLocaleString()} / {limit.toLocaleString()}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function ToolButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition hover:bg-raised hover:text-fg disabled:opacity-40"
    >
      {children}
    </button>
  );
}

const SWATCHES = [
  ["#5865F2", "Blurple"],
  ["#DEAC52", "Crusader gold"],
  ["#57F287", "Green"],
  ["#FEE75C", "Yellow"],
  ["#ED4245", "Red"],
  ["#EB459E", "Pink"],
  ["#9B59B6", "Purple"],
  ["#1ABC9C", "Teal"],
  ["#95A5A6", "Grey"],
];

/** Accent colour: a row of swatches plus a custom colour picker. */
export function ColorPicker({ value, onChange, fallback, disabled }: { value: string | null; onChange: (v: string | null) => void; fallback: string; disabled?: boolean }) {
  const current = (value ?? fallback).toUpperCase();
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {SWATCHES.map(([hex, name]) => (
        <button
          key={hex}
          type="button"
          disabled={disabled}
          title={name}
          aria-label={name}
          aria-pressed={current === hex}
          onClick={() => onChange(hex === fallback.toUpperCase() ? null : hex)}
          className={cx("h-6 w-6 rounded-full ring-offset-2 ring-offset-surface transition", current === hex ? "ring-2 ring-fg" : "hover:scale-110")}
          style={{ background: hex }}
        />
      ))}
      <label className="relative ml-1 flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-line px-2 text-xs text-muted hover:text-fg" title="Pick any colour">
        <span className="h-3.5 w-3.5 rounded-sm" style={{ background: current }} />
        <span className="font-mono">{current}</span>
        <input
          type="color"
          className="absolute inset-0 cursor-pointer opacity-0"
          value={current.toLowerCase()}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value.toUpperCase() === fallback.toUpperCase() ? null : e.target.value.toUpperCase())}
        />
      </label>
    </div>
  );
}
