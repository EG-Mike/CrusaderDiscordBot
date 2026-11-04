// Renders Discord-flavoured markdown as React elements, for live previews of
// what the bot will post. Builds elements only (never raw HTML), so text
// typed into the dashboard can't inject anything into the page.
import { Fragment, type ReactNode } from "react";
import type { Channel, Role } from "./api";

export interface MentionContext {
  roles?: Role[];
  channels?: Channel[];
}

type Rule = {
  re: RegExp;
  render: (m: RegExpExecArray, ctx: MentionContext, key: string) => ReactNode;
};

const linkClass = "text-[#00a8fc] hover:underline";
const mentionClass = "rounded-[3px] bg-[#5865f2]/30 px-0.5 font-medium text-[#c9cdfb]";

function formatTimestamp(seconds: number, style: string) {
  const date = new Date(seconds * 1000);
  switch (style) {
    case "t":
      return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    case "T":
      return date.toLocaleTimeString();
    case "d":
      return date.toLocaleDateString();
    case "D":
      return date.toLocaleDateString(undefined, { dateStyle: "long" });
    case "F":
      return date.toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" });
    case "R": {
      const diff = Math.round((seconds * 1000 - Date.now()) / 60000);
      const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
      if (Math.abs(diff) < 60) return rtf.format(diff, "minute");
      if (Math.abs(diff) < 1440) return rtf.format(Math.round(diff / 60), "hour");
      return rtf.format(Math.round(diff / 1440), "day");
    }
    default:
      return date.toLocaleString(undefined, { dateStyle: "long", timeStyle: "short" });
  }
}

/** Very dark role colours are unreadable on Discord's dark background. */
function readable(hex: string) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 70;
}

// Order matters when two rules match at the same position.
const RULES: Rule[] = [
  { re: /`([^`\n]+)`/, render: (m, _c, k) => <code key={k} className="rounded bg-[#1e1f22] px-1 py-px font-mono text-[0.85em]">{m[1]}</code> },
  {
    re: /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/,
    render: (m, c, k) => (
      <a key={k} href={m[2]} target="_blank" rel="noreferrer noopener" className={linkClass}>
        {inline(m[1], c)}
      </a>
    ),
  },
  {
    re: /<t:(-?\d{1,13})(?::([tTdDfFR]))?>/,
    render: (m, _c, k) => (
      <span key={k} className="rounded bg-[#1e1f22] px-1">
        {formatTimestamp(Number(m[1]), m[2] ?? "f")}
      </span>
    ),
  },
  {
    re: /<a?:(\w+):(\d+)>/,
    render: (m, _c, k) => (
      <img key={k} src={`https://cdn.discordapp.com/emojis/${m[2]}.webp?size=48`} alt={`:${m[1]}:`} title={`:${m[1]}:`} className="inline h-[1.375em] w-[1.375em] align-[-0.3em]" />
    ),
  },
  {
    re: /<@&(\d+)>/,
    render: (m, c, k) => {
      const role = c.roles?.find((r) => r.id === m[1]);
      const colored = role?.color && readable(role.color);
      return (
        <span key={k} className={mentionClass} style={colored ? { color: role!.color!, background: `${role!.color}26` } : undefined}>
          @{role?.name ?? "unknown-role"}
        </span>
      );
    },
  },
  {
    re: /<#(\d+)>/,
    render: (m, c, k) => (
      <span key={k} className={mentionClass}>
        #{c.channels?.find((ch) => ch.id === m[1])?.name ?? "unknown-channel"}
      </span>
    ),
  },
  { re: /<@!?(\d+)>/, render: (_m, _c, k) => <span key={k} className={mentionClass}>@member</span> },
  { re: /@(everyone|here)\b/, render: (m, _c, k) => <span key={k} className={mentionClass}>@{m[1]}</span> },
  {
    re: /https?:\/\/[^\s<]+[^\s<.,:;"')\]]/,
    render: (m, _c, k) => (
      <a key={k} href={m[0]} target="_blank" rel="noreferrer noopener" className={linkClass}>
        {m[0]}
      </a>
    ),
  },
  { re: /\*\*\*([\s\S]+?)\*\*\*/, render: (m, c, k) => <strong key={k}><em>{inline(m[1], c)}</em></strong> },
  { re: /\*\*([\s\S]+?)\*\*/, render: (m, c, k) => <strong key={k} className="font-semibold">{inline(m[1], c)}</strong> },
  { re: /__([\s\S]+?)__/, render: (m, c, k) => <u key={k}>{inline(m[1], c)}</u> },
  { re: /\*([^*\n]+?)\*/, render: (m, c, k) => <em key={k}>{inline(m[1], c)}</em> },
  { re: /(?<![\w])_([^_\n]+?)_(?![\w])/, render: (m, c, k) => <em key={k}>{inline(m[1], c)}</em> },
  { re: /~~([\s\S]+?)~~/, render: (m, c, k) => <s key={k}>{inline(m[1], c)}</s> },
  {
    re: /\|\|([\s\S]+?)\|\|/,
    render: (m, c, k) => (
      <span key={k} className="rounded bg-[#1e1f22] px-0.5" title="Spoiler (shown here so you can check it)">
        {inline(m[1], c)}
      </span>
    ),
  },
];

/** Inline markdown -> nodes: repeatedly takes the earliest matching rule. */
export function inline(text: string, ctx: MentionContext = {}): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest) {
    let best: { rule: Rule; m: RegExpExecArray } | null = null;
    for (const rule of RULES) {
      const m = rule.re.exec(rest);
      if (m && (best === null || m.index < best.m.index)) best = { rule, m };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.m.index > 0) out.push(rest.slice(0, best.m.index));
    out.push(best.rule.render(best.m, ctx, `i${n++}`));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  return out;
}

/** Block markdown (headings, subtext, quotes, lists, code blocks) -> nodes. */
export function DiscordMarkdown({ text, ctx = {} }: { text: string; ctx?: MentionContext }) {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];

    // ```code blocks```
    const fence = line.match(/^```(\w*)\s*$/) ?? line.match(/^```(.*)$/);
    if (fence) {
      const body: string[] = [];
      const firstInline = line.slice(3);
      const sameLineClose = firstInline.length > 3 && firstInline.endsWith("```");
      if (sameLineClose) {
        blocks.push(<pre key={key++} className="my-1 overflow-x-auto rounded bg-[#1e1f22] p-2 font-mono text-[0.85em]">{firstInline.slice(0, -3)}</pre>);
        i++;
        continue;
      }
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      i++;
      blocks.push(
        <pre key={key++} className="my-1 overflow-x-auto whitespace-pre rounded border border-[#1e1f22] bg-[#2b2d31] p-2 font-mono text-[0.85em] leading-snug">
          {body.join("\n")}
        </pre>,
      );
      continue;
    }

    // >>> quotes everything after it
    if (line.startsWith(">>> ")) {
      const rest = [line.slice(4), ...lines.slice(i + 1)].join("\n");
      blocks.push(
        <div key={key++} className="flex gap-2">
          <div className="w-1 shrink-0 rounded bg-[#4e5058]" />
          <div className="min-w-0">
            <DiscordMarkdown text={rest} ctx={ctx} />
          </div>
        </div>,
      );
      break;
    }

    // consecutive "> " lines form one quote
    if (/^> /.test(line) || line === ">") {
      const quoted: string[] = [];
      while (i < lines.length && (/^> /.test(lines[i]) || lines[i] === ">")) quoted.push(lines[i++].slice(2));
      blocks.push(
        <div key={key++} className="flex gap-2">
          <div className="w-1 shrink-0 rounded bg-[#4e5058]" />
          <div className="min-w-0">
            <DiscordMarkdown text={quoted.join("\n")} ctx={ctx} />
          </div>
        </div>,
      );
      continue;
    }

    const heading = line.match(/^(#{1,3}) (.+)$/);
    if (heading) {
      const size = heading[1].length === 1 ? "text-[1.5em] leading-tight" : heading[1].length === 2 ? "text-[1.25em] leading-tight" : "text-[1em]";
      blocks.push(
        <div key={key++} className={`${size} mb-1 mt-2 font-bold text-[#f2f3f5] first:mt-0`}>
          {inline(heading[2], ctx)}
        </div>,
      );
      i++;
      continue;
    }

    const subtext = line.match(/^-# (.+)$/);
    if (subtext) {
      blocks.push(
        <div key={key++} className="text-[0.8125em] text-[#949ba4]">
          {inline(subtext[1], ctx)}
        </div>,
      );
      i++;
      continue;
    }

    const item = line.match(/^(\s*)([-*]|\d+\.) (.+)$/);
    if (item) {
      const depth = Math.min(Math.floor(item[1].length / 2), 3);
      const marker = /\d/.test(item[2]) ? item[2] : depth ? "◦" : "•";
      blocks.push(
        <div key={key++} className="flex gap-1.5" style={{ paddingLeft: `${depth * 1.25 + 0.25}em` }}>
          <span className="shrink-0 text-[#b5bac1]">{marker}</span>
          <span className="min-w-0">{inline(item[3], ctx)}</span>
        </div>,
      );
      i++;
      continue;
    }

    blocks.push(
      <div key={key++} className="min-h-[1.375em]">
        {line ? inline(line, ctx) : <Fragment>&nbsp;</Fragment>}
      </div>,
    );
    i++;
  }
  return <div className="break-words">{blocks}</div>;
}
