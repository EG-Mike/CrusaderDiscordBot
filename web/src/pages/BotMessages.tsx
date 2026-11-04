import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle, History, MessageSquareText, Pin, RefreshCw, RotateCcw, Send } from "lucide-react";
import { ApiError, api, type BotMessage, type BotMessageSaveResult, type BotMessageVersion } from "../lib/api";
import { useGuildLookups } from "../lib/guild";
import { fullDateTime, timeAgo, useNow } from "../lib/time";
import { DiscordMarkdown } from "../lib/discordMarkdown";
import { EmbedPreview, MessageFrame } from "../components/DiscordPreview";
import { ColorPicker, MarkdownEditor } from "../components/MarkdownEditor";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Skeleton, cx } from "../components/ui";
import { who } from "./Announcements";

interface Draft {
  title: string;
  body: string;
  color: string | null;
}

const draftOf = (m: BotMessage | BotMessageVersion | BotMessage["default"]): Draft => ({ title: m.title ?? "", body: m.body, color: m.color });
const sameDraft = (a: Draft, b: Draft) => a.title === b.title && a.body === b.body && a.color === b.color;

/** Replaces {name} with the preview value; unknown names stay as typed. */
function fill(text: string, values: Record<string, string>) {
  return text.replace(/\{(\w+)\}/g, (all, name: string) => (name in values ? values[name] : all));
}

function unknownPlaceholders(text: string, known: string[]) {
  return [...new Set([...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).filter((n) => !known.includes(n)))];
}

function MessageList({ messages, selected, onSelect }: { messages: BotMessage[]; selected: string; onSelect: (key: string) => void }) {
  const groups = [...new Set(messages.map((m) => m.group))];
  return (
    <nav className="space-y-4">
      {groups.map((group) => (
        <div key={group}>
          <div className="label px-2.5 pb-1.5">{group}</div>
          <div className="space-y-0.5">
            {messages
              .filter((m) => m.group === group)
              .map((m) => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => onSelect(m.key)}
                  aria-current={selected === m.key}
                  className={cx(
                    "flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition",
                    selected === m.key ? "bg-gold/10 text-fg" : "text-muted hover:bg-raised hover:text-fg",
                  )}
                >
                  {m.kind === "embed" ? <Pin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" /> : <Send className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" />}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px]">{m.label}</span>
                    <span className="block truncate text-2xs text-faint">{m.where}</span>
                  </span>
                  {m.customized && <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-gold" title="Edited (not the default text)" />}
                </button>
              ))}
          </div>
        </div>
      ))}
    </nav>
  );
}

function Editor({ message, onSaved }: { message: BotMessage; onSaved: (m: BotMessage) => void }) {
  const toast = useToast();
  const now = useNow();
  const { roles, channels } = useGuildLookups();
  const [draft, setDraft] = useState<Draft>(draftOf(message));
  const [busy, setBusy] = useState<null | "save" | "push" | "reset" | "update">(null);
  const [dialog, setDialog] = useState<null | "reset" | "history">(null);
  const [history, setHistory] = useState<BotMessageVersion[] | null>(null);

  useEffect(() => setDraft(draftOf(message)), [message]);

  const isEmbed = message.kind === "embed";
  const dirty = !sameDraft(draft, draftOf(message));
  const values = Object.fromEntries(message.variables.map((v) => [v.name, v.value]));
  const unknown = unknownPlaceholders(`${draft.title} ${draft.body}`, message.variables.map((v) => v.name));
  const previewBody = fill(draft.body, values);
  const tooLong = previewBody.length > message.limit - 50;
  const ctx = { roles, channels };
  const isDefault = sameDraft(draft, draftOf(message.default));

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  const report = (result: BotMessageSaveResult, what: string) => {
    onSaved(result.message);
    if (result.discord_error) toast("error", `${what}, but Discord wasn't updated: ${result.discord_error}`);
    else toast("ok", result.discord ? `${what}. ${result.discord}` : what);
  };

  const save = async (push: boolean) => {
    setBusy(push ? "push" : "save");
    try {
      const result = await api.put<BotMessageSaveResult>(`/api/bot-messages/${message.key}${push ? "?update_discord=1" : ""}`, {
        title: draft.title,
        body: draft.body,
        color: draft.color,
      });
      report(result, "Saved");
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't save");
    } finally {
      setBusy(null);
    }
  };

  const reset = async () => {
    setBusy("reset");
    try {
      const result = await api.del<BotMessageSaveResult>(`/api/bot-messages/${message.key}${message.can_update_discord ? "?update_discord=1" : ""}`);
      report(result, "Back to the default text");
      setDialog(null);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't reset");
    } finally {
      setBusy(null);
    }
  };

  const updateDiscord = async () => {
    setBusy("update");
    try {
      const result = await api.post<{ discord: string }>(`/api/bot-messages/${message.key}/update-discord`);
      toast("ok", result.discord);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't update Discord");
    } finally {
      setBusy(null);
    }
  };

  const openHistory = () => {
    setDialog("history");
    setHistory(null);
    api.get<BotMessageVersion[]>(`/api/bot-messages/${message.key}/history`).then(setHistory).catch(() => setHistory([]));
  };

  return (
    <div className="grid min-w-0 gap-5 xl:grid-cols-2">
      <section className="card min-w-0 space-y-4 p-4 md:p-5">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold">{message.label}</h2>
              <Badge tone={message.customized ? "gold" : "neutral"}>{message.customized ? "Edited" : "Default text"}</Badge>
            </div>
            <p className="mt-1 text-muted">{message.description}</p>
            <p className="mt-1 text-2xs text-faint">
              {message.where}
              {message.updated_at && (
                <>
                  {" "}· changed {timeAgo(message.updated_at, now)} by {who(message.updated_by)}
                </>
              )}
            </p>
          </div>
          <Button size="sm" variant="ghost" icon={<History className="h-3.5 w-3.5" />} onClick={openHistory}>
            History
          </Button>
        </div>

        {!message.feature_enabled && (
          <div className="flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/[0.06] px-3 py-2 text-muted">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warn" /> Its feature is switched off, so this message isn't in use right now.
          </div>
        )}

        {isEmbed && (
          <div>
            <label className="label mb-1.5 block" htmlFor="bm-title">
              Title
            </label>
            <input id="bm-title" className="input" value={draft.title} maxLength={256} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
          </div>
        )}
        <div>
          <span className="label mb-1.5 block">Text</span>
          <MarkdownEditor
            label={`${message.label} text`}
            value={draft.body}
            onChange={(body) => setDraft({ ...draft, body })}
            limit={message.limit - 50}
            roles={roles}
            channels={channels}
            placeholders={message.variables}
            rows={isEmbed ? 16 : 9}
          />
          {message.variables.length > 0 && (
            <p className="mt-1.5 text-2xs text-faint">
              Click a <span className="font-mono text-gold">{"{placeholder}"}</span> to insert it; the bot fills it in when it posts.{" "}
              {message.variables.map((v) => `{${v.name}}: ${v.description.toLowerCase()}`).join(" · ")}
            </p>
          )}
          {unknown.length > 0 && (
            <p className="mt-1.5 flex items-center gap-1.5 text-xs text-warn">
              <AlertTriangle className="h-3.5 w-3.5" />
              {unknown.map((n) => `{${n}}`).join(", ")} {unknown.length === 1 ? "isn't a placeholder" : "aren't placeholders"} for this message and will show as typed.
            </p>
          )}
        </div>
        {isEmbed && (
          <div>
            <span className="label mb-1.5 block">Colour</span>
            <ColorPicker value={draft.color} fallback={message.default_color} onChange={(color) => setDraft({ ...draft, color })} />
          </div>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
          {!isDefault && (
            <Button variant="ghost" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => setDialog("reset")} disabled={!!busy} className="mr-auto">
              Reset to default
            </Button>
          )}
          {dirty && (
            <Button variant="ghost" onClick={() => setDraft(draftOf(message))} disabled={!!busy}>
              Discard
            </Button>
          )}
          {isEmbed && !dirty && message.can_update_discord && (
            <Button variant="outline" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={updateDiscord} loading={busy === "update"} title="Edit the pinned message to match this text, or post it if it's missing">
              Update in Discord
            </Button>
          )}
          <Button variant={isEmbed && message.can_update_discord ? "outline" : "primary"} onClick={() => save(false)} disabled={!dirty || tooLong || !draft.body.trim()} loading={busy === "save"}>
            Save
          </Button>
          {isEmbed && message.can_update_discord && (
            <Button variant="primary" onClick={() => save(true)} disabled={!dirty || tooLong || !draft.body.trim()} loading={busy === "push"}>
              Save and update in Discord
            </Button>
          )}
        </div>
        <p className="text-2xs text-faint">
          {isEmbed
            ? message.can_update_discord
              ? "Saving only stores the text; the pinned message changes when you update it in Discord, or at the next restart."
              : "The pinned message is updated to this text the next time the bot starts with this feature on."
            : "Used from the next time the bot sends this message."}
        </p>
      </section>

      <section className="min-w-0 xl:sticky xl:top-4 xl:self-start">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">Preview</span>
          {message.variables.length > 0 && <span className="text-2xs text-faint">Placeholders shown with example values</span>}
        </div>
        <MessageFrame>
          {isEmbed ? (
            <EmbedPreview title={fill(draft.title, values) || null} body={previewBody} color={draft.color ?? message.default_color} footer={message.footer} ctx={ctx} />
          ) : (
            <DiscordMarkdown text={previewBody} ctx={ctx} />
          )}
        </MessageFrame>
        {!isEmbed && <p className="mt-2 text-2xs text-faint">Sent as a direct message.</p>}
      </section>

      <Dialog
        open={dialog === "reset"}
        onClose={() => setDialog(null)}
        busy={busy === "reset"}
        title="Go back to the default text?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={reset} loading={busy === "reset"}>
              Reset to default
            </Button>
          </>
        }
      >
        <p className="text-muted">
          The edited text is replaced by the bot's built-in text{message.can_update_discord ? ", and the pinned message is updated" : ""}. The edited version stays in the
          history, so you can load it again.
        </p>
      </Dialog>

      <Dialog open={dialog === "history"} onClose={() => setDialog(null)} title={`History: ${message.label}`} wide>
        {history === null ? (
          <Skeleton className="h-24" />
        ) : history.length === 0 ? (
          <p className="text-muted">Never changed: this is still the default text.</p>
        ) : (
          <ul className="divide-y divide-line">
            {history.map((v) => (
              <li key={v.id} className="flex items-center gap-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="truncate">{v.body.split("\n")[0] || "(empty)"}</div>
                  <div className="text-2xs text-faint">
                    {fullDateTime(v.saved_at)} · {who(v.saved_by)}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<RotateCcw className="h-3.5 w-3.5" />}
                  onClick={() => {
                    setDraft(draftOf(v));
                    setDialog(null);
                    toast("ok", "Loaded into the editor. Save to use it.");
                  }}
                >
                  Load
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Dialog>
    </div>
  );
}

export default function BotMessagesPage() {
  const [messages, setMessages] = useState<BotMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();

  const load = useCallback(() => {
    api.get<BotMessage[]>("/api/bot-messages").then(setMessages).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load the messages"));
  }, []);
  useEffect(load, [load]);

  const selectedKey = params.get("key") ?? messages?.[0]?.key ?? "";
  const selected = useMemo(() => messages?.find((m) => m.key === selectedKey) ?? messages?.[0], [messages, selectedKey]);
  const select = (key: string) => setParams({ key }, { replace: true });

  return (
    <>
      <SectionHeader
        eyebrow="Communication"
        title="Bot messages"
        description="The texts the bot posts on its own: pinned explainers and the DMs applicants get. Edit them here; placeholders are filled in when the bot sends them."
      />
      {error ? (
        <div className="card">
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load the messages">
            {error}
          </EmptyState>
        </div>
      ) : !messages || !selected ? (
        <div className="grid gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <Skeleton className="h-72" />
          <Skeleton className="h-[480px]" />
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <div className="lg:hidden">
            <label className="label mb-1.5 block" htmlFor="bm-select">
              Message
            </label>
            <select id="bm-select" className="input" value={selected.key} onChange={(e) => select(e.target.value)}>
              {[...new Set(messages.map((m) => m.group))].map((group) => (
                <optgroup key={group} label={group}>
                  {messages
                    .filter((m) => m.group === group)
                    .map((m) => (
                      <option key={m.key} value={m.key}>
                        {m.label}
                        {m.customized ? " (edited)" : ""}
                      </option>
                    ))}
                </optgroup>
              ))}
            </select>
          </div>
          <aside className="hidden lg:block">
            <MessageList messages={messages} selected={selected.key} onSelect={select} />
            <p className="mt-4 flex items-start gap-2 px-2.5 text-2xs text-faint">
              <MessageSquareText className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>
                <Pin className="inline h-3 w-3" /> pinned in a channel · <Send className="inline h-3 w-3" /> sent as a DM ·{" "}
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-gold align-middle" /> edited
              </span>
            </p>
          </aside>
          <Editor
            key={selected.key}
            message={selected}
            onSaved={(updated) => setMessages((list) => list && list.map((m) => (m.key === updated.key ? updated : m)))}
          />
        </div>
      )}
    </>
  );
}
