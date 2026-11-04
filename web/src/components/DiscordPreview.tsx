// Discord-style previews of what the bot will post: the message frame (bot
// name, APP tag, time) around an announcement container, an embed, or plain
// text. Colours follow Discord's dark theme so the preview reads like the
// real thing.
import type { ReactNode } from "react";
import { DiscordMarkdown, type MentionContext } from "../lib/discordMarkdown";
import { useLive } from "../lib/live";

export const DISCORD_BLURPLE = "#5865F2";

export function MessageFrame({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  const { status } = useLive();
  const name = status?.bot.name?.split("#")[0] ?? "Crusader";
  const time = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="rounded-lg bg-[#313338] p-4 text-[15px] leading-[1.375] text-[#dbdee1]" style={{ fontFamily: "'gg sans', 'Noto Sans', Inter, sans-serif" }}>
      <div className="flex gap-3">
        {status?.bot.avatar_url ? (
          <img src={status.bot.avatar_url} alt="" className="h-10 w-10 shrink-0 rounded-full" />
        ) : (
          <div className="h-10 w-10 shrink-0 rounded-full bg-[#5865f2]" />
        )}
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex flex-wrap items-center gap-1.5">
            <span className="font-medium text-[#f2f3f5]">{name}</span>
            <span className="rounded bg-[#5865f2] px-1 text-[10px] font-semibold leading-[15px] text-white">APP</span>
            <span className="text-xs text-[#949ba4]">Today at {time}</span>
          </div>
          {children}
          {footer}
        </div>
      </div>
    </div>
  );
}

/** A components-v2 container: accent bar, title heading, sections split on '---'. */
export function ContainerPreview({
  title,
  body,
  color,
  ping,
  ctx,
}: {
  title: string | null;
  body: string;
  color: string | null;
  ping?: string | null;
  ctx: MentionContext;
}) {
  const sections = splitSections(body);
  return (
    <>
      {ping && (
        <div className="mb-1">
          <DiscordMarkdown text={ping} ctx={ctx} />
        </div>
      )}
      <div className="flex max-w-[600px] overflow-hidden rounded-lg border border-[#3f4147]/60 bg-[#393a41]/40">
        <div className="w-1 shrink-0" style={{ background: color ?? DISCORD_BLURPLE }} />
        <div className="min-w-0 flex-1 space-y-2 p-4">
          {title && (
            <>
              <DiscordMarkdown text={`## ${title}`} ctx={ctx} />
              <Separator />
            </>
          )}
          {sections.length ? (
            sections.map((section, i) => (
              <div key={i} className="space-y-2">
                <DiscordMarkdown text={section} ctx={ctx} />
                {i < sections.length - 1 && <Separator />}
              </div>
            ))
          ) : (
            <div className="italic text-[#949ba4]">Nothing written yet.</div>
          )}
        </div>
      </div>
    </>
  );
}

function Separator() {
  return <div className="h-px bg-[#4e5058]/60" />;
}

export function splitSections(body: string) {
  const sections: string[] = [];
  let current: string[] = [];
  for (const line of body.split("\n")) {
    if (line.trim() === "---") {
      sections.push(current.join("\n").trim());
      current = [];
    } else current.push(line);
  }
  sections.push(current.join("\n").trim());
  return sections.filter(Boolean);
}

/** A classic embed: left colour bar, bold title, description, footer. */
export function EmbedPreview({
  title,
  body,
  color,
  footer,
  ctx,
}: {
  title: string | null;
  body: string;
  color: string | null;
  footer?: string | null;
  ctx: MentionContext;
}) {
  return (
    <div className="flex max-w-[520px] overflow-hidden rounded bg-[#2b2d31]">
      <div className="w-1 shrink-0" style={{ background: color ?? DISCORD_BLURPLE }} />
      <div className="min-w-0 flex-1 px-4 pb-4 pt-2 text-[14px]">
        {title && <div className="mt-1 font-semibold text-[#f2f3f5]">{title}</div>}
        <div className="mt-2">
          <DiscordMarkdown text={body} ctx={ctx} />
        </div>
        {footer && <div className="mt-2 text-xs text-[#949ba4]">{footer}</div>}
      </div>
    </div>
  );
}
