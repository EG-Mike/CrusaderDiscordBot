import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, Clock, ExternalLink, Hash, Lock, Plus, Trash2, Users, Vote, X } from "lucide-react";
import { ApiError, api, type Channel, type Poll, type PollList, type PollPhase, type PollSettings } from "../lib/api";
import { useGuildLookups } from "../lib/guild";
import { useLive } from "../lib/live";
import { useNow } from "../lib/time";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, SectionHeader, Segmented, Skeleton, Toggle, cx } from "../components/ui";

const PHASES: Record<PollPhase, { label: string; tone: "info" | "gold" | "neutral" }> = {
  adding: { label: "Collecting options", tone: "info" },
  voting: { label: "Voting", tone: "gold" },
  closed: { label: "Closed", tone: "neutral" },
};

// Same wording as the poll's Manage panel in Discord. Privacy settings can
// only be switched off after posting.
const TOGGLES: { key: keyof PollSettings; label: string; help: string; oneWay?: boolean }[] = [
  { key: "members_can_add", label: "Members can add options", help: "Anyone can suggest options, up to the limit below." },
  { key: "live_results", label: "Live results", help: "Show the tally while voting is open (off: revealed when closed)." },
  { key: "allow_vote_change", label: "Vote changes", help: "Members can change or withdraw their vote." },
  { key: "show_authors", label: "Show who added options", help: "Can't be switched back on after posting.", oneWay: true },
  { key: "show_voters", label: "Public votes", help: "Everyone sees who voted for what. Can't be switched back on after posting.", oneWay: true },
];

const DURATIONS = [
  ["", "No deadline"],
  ["12h", "12 hours"],
  ["1d", "1 day"],
  ["2d", "2 days"],
  ["3d", "3 days"],
  ["7d", "1 week"],
] as const;

const DEADLINE_PRESETS = [
  [3600, "1 hour"],
  [6 * 3600, "6 hours"],
  [12 * 3600, "12 hours"],
  [86400, "1 day"],
  [2 * 86400, "2 days"],
  [3 * 86400, "3 days"],
  [7 * 86400, "1 week"],
] as const;

/** A <input type="datetime-local"> value -> unix seconds (browser's time zone). */
function localToUnix(value: string) {
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

/** Earliest selectable value for a datetime-local input (now + 5 minutes). */
function minLocal() {
  const d = new Date(Date.now() + 5 * 60000 - new Date().getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}

function DeadlinePanel({ poll, act, busy }: { poll: Poll; act: (action: string, extra?: Record<string, unknown>, ok?: string) => Promise<boolean>; busy: string | null }) {
  const [mode, setMode] = useState<"in" | "at">("in");
  const [inSeconds, setInSeconds] = useState(86400);
  const [at, setAt] = useState("");
  const [message, setMessage] = useState("");
  const [announce, setAnnounce] = useState(true);
  const closesAt = mode === "in" ? Math.floor(Date.now() / 1000) + inSeconds : localToUnix(at);

  const submit = async () => {
    if (!closesAt) return;
    const ok = await act("set_deadline", { closes_at: closesAt, message, announce }, announce ? `Deadline set and announced in #${poll.channel_name}` : "Deadline set");
    if (ok) setMessage("");
  };

  return (
    <div className="border-t border-line px-4 py-3 md:px-5">
      <div className="label mb-1">{poll.closes_at ? "Change the deadline" : "Set a deadline"}</div>
      {poll.closes_at && (
        <p className="mb-2 text-xs text-muted">
          Currently closes {new Date(poll.closes_at * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
          {poll.phase === "adding" && " (once voting is open)"}.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Segmented label="Deadline type" value={mode} onChange={setMode} options={[["in", "Close in"], ["at", "Close at"]] as const} />
        {mode === "in" ? (
          <select className="input h-8 w-auto py-1 text-xs" value={inSeconds} onChange={(e) => setInSeconds(Number(e.target.value))} aria-label="Close in">
            {DEADLINE_PRESETS.map(([seconds, label]) => (
              <option key={seconds} value={seconds}>
                {label}
              </option>
            ))}
          </select>
        ) : (
          <input type="datetime-local" className="input h-8 w-auto py-1 text-xs" min={minLocal()} value={at} onChange={(e) => setAt(e.target.value)} aria-label="Close at" />
        )}
      </div>
      <textarea
        className="input mt-2 text-xs"
        rows={2}
        maxLength={500}
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="Optional message posted with it, e.g. Last chance to vote for the raid night!"
        aria-label="Message"
        disabled={!announce}
      />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-muted">
          <Toggle checked={announce} onChange={setAnnounce} label="Announce in the channel" /> Announce in #{poll.channel_name ?? "the channel"}
        </label>
        <Button size="sm" variant="primary" className="ml-auto" onClick={submit} loading={busy === "set_deadline"} disabled={!closesAt}>
          {poll.closes_at ? "Change deadline" : "Set deadline"}
        </Button>
      </div>
      {announce && closesAt && (
        <p className="mt-2 rounded-md border border-line bg-bg/50 px-2.5 py-1.5 text-2xs text-muted">
          The bot posts: ⏰ Voting on <b>{poll.question}</b> ends {new Date(closesAt * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}.
          {message.trim() && <span className="block">{message.trim()}</span>}
        </p>
      )}
    </div>
  );
}

function deadlineLabel(poll: Poll, now: number) {
  if (poll.phase !== "voting" || !poll.closes_at) return null;
  const minutes = Math.round((poll.closes_at * 1000 - now) / 60000);
  if (minutes <= 0) return "Closing now";
  if (minutes < 60) return `Closes in ${minutes}m`;
  if (minutes < 48 * 60) return `Closes in ${Math.round(minutes / 60)}h`;
  return `Closes in ${Math.round(minutes / 1440)}d`;
}

function Results({ poll }: { poll: Poll }) {
  const total = poll.options.reduce((n, o) => n + o.votes, 0);
  const top = Math.max(0, ...poll.options.map((o) => o.votes));
  const ordered = poll.phase === "closed" ? [...poll.options].sort((a, b) => b.votes - a.votes) : poll.options;
  if (!poll.options.length) return <p className="text-muted">No options yet.</p>;
  return (
    <ul className="space-y-2.5">
      {ordered.map((o) => {
        const share = total ? o.votes / total : 0;
        const winner = o.votes === top && top > 0;
        return (
          <li key={o.id}>
            <div className="mb-1 flex items-baseline justify-between gap-3">
              <span className={cx("min-w-0 truncate", winner ? "font-semibold text-fg" : "text-muted")}>{o.text}</span>
              <span className="shrink-0 text-xs tabular-nums text-faint">
                {o.votes} vote{o.votes === 1 ? "" : "s"} · {Math.round(share * 100)}%
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-raised">
              <div className={cx("h-full rounded-full transition-all", winner ? "bg-gold" : "bg-faint/60")} style={{ width: `${Math.max(share * 100, o.votes ? 2 : 0)}%` }} />
            </div>
            {(o.author_name && poll.settings.show_authors) || o.voters.length ? (
              <p className="mt-1 text-2xs text-faint">
                {poll.settings.show_authors && o.author_name && <>Added by {o.author_name}</>}
                {o.voters.length > 0 && <>{poll.settings.show_authors && o.author_name ? " · " : ""}Voted: {o.voters.join(", ")}</>}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function PollDetail({ poll, onChanged, onDeleted }: { poll: Poll; onChanged: (p: Poll) => void; onDeleted: () => void }) {
  const toast = useToast();
  const { status } = useLive();
  const now = useNow();
  const [busy, setBusy] = useState<string | null>(null);
  const [newOption, setNewOption] = useState("");
  const [removing, setRemoving] = useState<number | null>(null);
  const [confirm, setConfirm] = useState<null | "close" | "delete">(null);

  const act = async (action: string, extra: Record<string, unknown> = {}, ok?: string) => {
    setBusy(action + (extra.key ?? ""));
    try {
      const updated = await api.post<Poll>(`/api/polls/${poll.message_id}/action`, { action, ...extra });
      onChanged(updated);
      if (ok) toast("ok", ok);
      return true;
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That didn't work");
      return false;
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("delete");
    try {
      await api.del(`/api/polls/${poll.message_id}`);
      toast("ok", "Poll deleted");
      onDeleted();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't delete the poll");
    } finally {
      setBusy(null);
      setConfirm(null);
    }
  };

  const link = status?.guild ? `https://discord.com/channels/${status.guild.id}/${poll.channel_id}/${poll.message_id}` : null;
  const s = poll.settings;
  const deadline = deadlineLabel(poll, now);

  return (
    <section className="card min-w-0">
      <header className="border-b border-line p-4 md:p-5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={PHASES[poll.phase].tone}>{PHASES[poll.phase].label}</Badge>
          {deadline && (
            <span className="inline-flex items-center gap-1 text-xs text-muted">
              <Clock className="h-3 w-3" /> {deadline}
            </span>
          )}
          {link && (
            <a href={link} target="_blank" rel="noreferrer" className="ml-auto inline-flex items-center gap-1 text-xs text-gold hover:underline">
              Open in Discord <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
        <h2 className="mt-2 text-base font-semibold">{poll.question}</h2>
        {poll.description && <p className="mt-1 whitespace-pre-line text-muted">{poll.description}</p>}
        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 text-2xs text-faint">
          <span className="inline-flex items-center">
            <Hash className="h-3 w-3" />
            {poll.channel_name ?? "unknown channel"}
          </span>
          <span>by {poll.created_by_name ?? "someone"}</span>
          <span className="inline-flex items-center gap-1">
            <Users className="h-3 w-3" /> {poll.voter_count} voted
          </span>
          {!poll.results_public && poll.phase !== "closed" && <span>Members can't see the results yet; you can.</span>}
        </p>
      </header>

      <div className="p-4 md:p-5">
        <Results poll={poll} />
      </div>

      <div className="flex flex-wrap gap-2 border-t border-line px-4 py-3 md:px-5">
        {poll.phase === "adding" && (
          <Button size="sm" variant="primary" onClick={() => act("open_voting", {}, "Voting is open")} loading={busy === "open_voting"}>
            Open voting
          </Button>
        )}
        {poll.phase === "voting" && (
          <Button size="sm" variant="danger" onClick={() => setConfirm("close")} loading={busy === "close"}>
            Close and announce result
          </Button>
        )}
        {poll.phase === "voting" && s.members_can_add && s.collect_first && (
          <Button size="sm" onClick={() => act("reopen_adding", {}, "Adding reopened")} loading={busy === "reopen_adding"}>
            Reopen adding
          </Button>
        )}
        {poll.phase === "closed" && (
          <Button size="sm" onClick={() => act("reopen_voting", {}, "Voting reopened")} loading={busy === "reopen_voting"}>
            Reopen voting
          </Button>
        )}
        {poll.phase === "voting" && poll.closes_at && (
          <Button size="sm" variant="ghost" onClick={() => act("clear_deadline", {}, "Deadline removed")} loading={busy === "clear_deadline"}>
            Remove deadline
          </Button>
        )}
        <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => setConfirm("delete")} className="ml-auto">
          Delete
        </Button>
      </div>

      {poll.phase !== "closed" && <DeadlinePanel poll={poll} act={act} busy={busy} />}

      {poll.phase !== "closed" && (
        <div className="border-t border-line px-4 py-3 md:px-5">
          <div className="label mb-2">Options</div>
          <ul className="mb-2 flex flex-wrap gap-1.5">
            {poll.options.map((o) => (
              <li key={o.id} className="inline-flex items-center gap-1 rounded-md border border-line bg-bg/50 py-0.5 pl-2 pr-1 text-xs">
                {o.text}
                <button type="button" onClick={() => setRemoving(o.id)} className="rounded p-0.5 text-faint hover:bg-crimson/10 hover:text-crimson" aria-label={`Remove ${o.text}`}>
                  <X className="h-3 w-3" />
                </button>
              </li>
            ))}
          </ul>
          <form
            className="flex gap-2"
            onSubmit={async (e) => {
              e.preventDefault();
              if (newOption.trim() && (await act("add_option", { text: newOption }, "Option added"))) setNewOption("");
            }}
          >
            <input className="input h-8 py-1 text-xs" placeholder="Add an option…" maxLength={55} value={newOption} onChange={(e) => setNewOption(e.target.value)} aria-label="New option" />
            <Button size="sm" type="submit" icon={<Plus className="h-3.5 w-3.5" />} loading={busy === "add_option"} disabled={!newOption.trim()}>
              Add
            </Button>
          </form>
        </div>
      )}

      <div className="border-t border-line px-4 py-3 md:px-5">
        <div className="label mb-1">Settings</div>
        <ul className="divide-y divide-line">
          {TOGGLES.map((t) => {
            const locked = t.oneWay && !s[t.key];
            const hidden = t.key === "members_can_add" && poll.phase === "closed";
            if (hidden) return null;
            return (
              <li key={t.key} className="flex items-center gap-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="text-[13px]">{t.label}</div>
                  <div className="text-2xs text-faint">{locked ? "Switched off for good: people took part under the promise of privacy." : t.help}</div>
                </div>
                {locked ? (
                  <Lock className="h-3.5 w-3.5 text-faint" />
                ) : (
                  <Toggle checked={Boolean(s[t.key])} label={t.label} disabled={busy !== null} onChange={(v) => act("toggle", { key: t.key, value: v })} />
                )}
              </li>
            );
          })}
        </ul>
        <p className="mt-1 text-2xs text-faint">
          {s.max_votes === 1 ? "One vote per member" : `Up to ${s.max_votes} votes per member`}
          {s.members_can_add && ` · up to ${s.max_adds} option${s.max_adds === 1 ? "" : "s"} added per member`}
        </p>
      </div>

      <Dialog
        open={confirm === "close"}
        onClose={() => setConfirm(null)}
        title="Close the poll?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={async () => {
                setConfirm(null);
                await act("close", {}, "Poll closed; the result was posted");
              }}
            >
              Close poll
            </Button>
          </>
        }
      >
        <p className="text-muted">Voting stops and the bot replies to the poll with the result. You can reopen voting later.</p>
      </Dialog>
      <Dialog
        open={confirm === "delete"}
        onClose={() => setConfirm(null)}
        busy={busy === "delete"}
        title="Delete the poll?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove} loading={busy === "delete"}>
              Delete poll
            </Button>
          </>
        }
      >
        <p className="text-muted">The poll message and all its votes are deleted. This can't be undone.</p>
      </Dialog>
      <Dialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Remove this option?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={async () => {
                const id = removing;
                setRemoving(null);
                await act("remove_options", { ids: [id] }, "Option removed");
              }}
            >
              Remove
            </Button>
          </>
        }
      >
        <p className="text-muted">“{poll.options.find((o) => o.id === removing)?.text}” is removed, along with any votes for it.</p>
      </Dialog>
    </section>
  );
}

function NewPollDialog({ open, onClose, onCreated, defaults, channels }: {
  open: boolean;
  onClose: () => void;
  onCreated: (p: Poll) => void;
  defaults: PollList["defaults"] | null;
  channels: Channel[];
}) {
  const toast = useToast();
  const [question, setQuestion] = useState("");
  const [description, setDescription] = useState("");
  const [options, setOptions] = useState("");
  const [channelId, setChannelId] = useState("");
  const [duration, setDuration] = useState("");
  const [closeAt, setCloseAt] = useState("");
  const [settings, setSettings] = useState<Partial<PollSettings>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open && defaults) setSettings({ ...defaults, members_can_add: false });
  }, [open, defaults]);

  const optionList = options.split("\n").map((o) => o.trim()).filter(Boolean);
  const usable = channels.filter((c) => c.type === "text" || c.type === "news");
  const set = (key: keyof PollSettings, value: boolean | number) => setSettings((s) => ({ ...s, [key]: value }));

  const create = async () => {
    setBusy(true);
    try {
      const timing = duration === "at" ? { closes_at: localToUnix(closeAt) } : { duration };
      const poll = await api.post<Poll>("/api/polls", { question, description, options: optionList, channel_id: channelId, settings: { ...settings, ...timing } });
      toast("ok", `Poll posted in #${poll.channel_name}`);
      onCreated(poll);
      setQuestion("");
      setDescription("");
      setOptions("");
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't post the poll");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={busy}
      wide
      title="New poll"
      actions={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={create} loading={busy} disabled={!question.trim() || !channelId || (!settings.members_can_add && optionList.length < 2) || (duration === "at" && !closeAt)}>
            Post poll
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="label mb-1.5 block" htmlFor="poll-q">Question</label>
          <input id="poll-q" className="input" maxLength={200} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. Which night for the extra raid?" autoFocus />
        </div>
        <div>
          <label className="label mb-1.5 block" htmlFor="poll-d">Description <span className="normal-case tracking-normal text-faint">(optional)</span></label>
          <textarea id="poll-d" className="input" rows={2} maxLength={400} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label mb-1.5 block" htmlFor="poll-o">Options <span className="normal-case tracking-normal text-faint">(one per line)</span></label>
            <textarea id="poll-o" className="input font-mono text-xs" rows={6} value={options} onChange={(e) => setOptions(e.target.value)} placeholder={"Wednesday\nThursday\nSunday"} />
            <p className="mt-1 text-2xs text-faint">{optionList.length} option{optionList.length === 1 ? "" : "s"} · at most 25, 55 characters each</p>
          </div>
          <div className="space-y-3">
            <div>
              <label className="label mb-1.5 block" htmlFor="poll-c">Channel</label>
              <select id="poll-c" className="input" value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                <option value="">Choose a channel…</option>
                {usable.map((c) => (
                  <option key={c.id} value={c.id} disabled={!c.bot_can_send}>
                    #{c.name}
                    {!c.bot_can_send ? " (bot can't post here)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label mb-1.5 block" htmlFor="poll-t">Voting closes</label>
              <select id="poll-t" className="input" value={duration} onChange={(e) => setDuration(e.target.value)}>
                {DURATIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                    {value ? " after voting opens" : ""}
                  </option>
                ))}
                <option value="at">At a date and time…</option>
              </select>
              {duration === "at" && (
                <input type="datetime-local" className="input mt-2" min={minLocal()} value={closeAt} onChange={(e) => setCloseAt(e.target.value)} aria-label="Voting closes at" />
              )}
            </div>
            <div>
              <label className="label mb-1.5 block" htmlFor="poll-v">Votes per member</label>
              <input id="poll-v" type="number" min={1} max={25} className="input" value={settings.max_votes ?? 1} onChange={(e) => set("max_votes", Math.max(1, Math.min(25, Number(e.target.value) || 1)))} />
            </div>
          </div>
        </div>
        <ul className="divide-y divide-line rounded-lg border border-line px-3">
          {TOGGLES.map((t) => (
            <li key={t.key} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="text-[13px]">{t.label}</div>
                <div className="text-2xs text-faint">{t.help.replace(" Can't be switched back on after posting.", "")}</div>
              </div>
              <Toggle checked={Boolean(settings[t.key])} label={t.label} onChange={(v) => set(t.key, v)} />
            </li>
          ))}
          {settings.members_can_add && (
            <li className="flex items-center gap-3 py-2">
              <label className="min-w-0 flex-1" htmlFor="poll-adds">
                <div className="text-[13px]">Options each member may add</div>
                <div className="text-2xs text-faint">The limit for suggestions per member.</div>
              </label>
              <input id="poll-adds" type="number" min={1} max={25} className="input h-8 w-20 py-1" value={settings.max_adds ?? 1} onChange={(e) => set("max_adds", Math.max(1, Math.min(25, Number(e.target.value) || 1)))} />
            </li>
          )}
          {settings.members_can_add && (
            <li className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="text-[13px]">Collect options before voting</div>
                <div className="text-2xs text-faint">Off: members add options and vote at the same time.</div>
              </div>
              <Toggle checked={Boolean(settings.collect_first)} label="Collect options first" onChange={(v) => set("collect_first", v)} />
            </li>
          )}
        </ul>
      </div>
    </Dialog>
  );
}

export default function PollsPage() {
  const [data, setData] = useState<PollList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const [creating, setCreating] = useState(false);
  const { subscribe } = useLive();
  const { channels } = useGuildLookups();
  const now = useNow();
  const tab = params.get("tab") === "closed" ? "closed" : "open";

  const load = useCallback(() => {
    api.get<PollList>("/api/polls").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load polls"));
  }, []);
  useEffect(load, [load]);
  // Votes and option changes made in Discord arrive as audit events.
  useEffect(
    () =>
      subscribe((e) => {
        if (e.type === "polls.changed") load();
        const event = e.event as { details?: { custom_id?: string }; kind?: string } | undefined;
        if (e.type === "audit.event" && (String(event?.details?.custom_id ?? "").startsWith("poll_") || event?.kind === "modal")) window.setTimeout(load, 600);
      }),
    [subscribe, load],
  );

  const polls = useMemo(() => (data?.polls ?? []).filter((p) => (tab === "closed" ? p.phase === "closed" : p.phase !== "closed")), [data, tab]);
  const selectedId = params.get("id") ?? polls[0]?.message_id;
  const selected = data?.polls.find((p) => p.message_id === selectedId) ?? null;
  const counts = { open: data?.polls.filter((p) => p.phase !== "closed").length ?? 0, closed: data?.polls.filter((p) => p.phase === "closed").length ?? 0 };

  const replace = (poll: Poll) => setData((d) => d && { ...d, polls: d.polls.some((p) => p.message_id === poll.message_id) ? d.polls.map((p) => (p.message_id === poll.message_id ? poll : p)) : [poll, ...d.polls] });

  return (
    <>
      <SectionHeader
        eyebrow="Communication"
        title="Polls"
        description="Start polls, follow the votes and manage them. Polls made with /poll create show up here too."
        actions={
          <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)} disabled={data !== null && !data.feature_enabled}>
            New poll
          </Button>
        }
      />
      {data && !data.feature_enabled && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1 text-muted">Polls are switched off.</span>
          <Link to="/settings?group=features#FEATURE_POLLS_ENABLED" className="text-xs text-gold hover:underline">
            Turn them on
          </Link>
        </div>
      )}
      {error ? (
        <div className="card">
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load polls">
            {error}
          </EmptyState>
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
          <section className="card min-w-0 self-start">
            <div className="border-b border-line p-3">
              <Segmented
                label="Show"
                value={tab}
                onChange={(t) => setParams(t === "open" ? {} : { tab: t }, { replace: true })}
                options={[
                  ["open", `Open · ${counts.open}`],
                  ["closed", `Closed · ${counts.closed}`],
                ] as const}
              />
            </div>
            {!data ? (
              <div className="space-y-2 p-3">
                {[0, 1, 2].map((i) => (
                  <Skeleton key={i} className="h-14" />
                ))}
              </div>
            ) : polls.length === 0 ? (
              <EmptyState icon={<Vote className="h-6 w-6" />} title={tab === "open" ? "No open polls" : "No closed polls"}>
                {tab === "open" ? "Start one with New poll, or /poll create in Discord." : "Closed polls and their results show up here."}
              </EmptyState>
            ) : (
              <ul className="divide-y divide-line">
                {polls.map((p) => (
                  <li key={p.message_id}>
                    <button
                      type="button"
                      onClick={() => setParams({ ...(tab === "closed" ? { tab } : {}), id: p.message_id }, { replace: true })}
                      className={cx("block w-full px-4 py-3 text-left transition hover:bg-raised/50", p.message_id === selected?.message_id && "bg-gold/[0.06]")}
                    >
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate font-medium">{p.question}</span>
                        <Badge tone={PHASES[p.phase].tone}>{PHASES[p.phase].label}</Badge>
                      </div>
                      <div className="mt-0.5 flex gap-2 text-2xs text-faint">
                        <span>#{p.channel_name ?? "?"}</span>
                        <span>{p.voter_count} voted</span>
                        {deadlineLabel(p, now) && <span>{deadlineLabel(p, now)}</span>}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
          {selected ? (
            <PollDetail key={selected.message_id} poll={selected} onChanged={replace} onDeleted={() => { setParams(tab === "closed" ? { tab } : {}, { replace: true }); load(); }} />
          ) : (
            data && polls.length > 0 && <div />
          )}
        </div>
      )}
      <NewPollDialog
        open={creating}
        onClose={() => setCreating(false)}
        defaults={data?.defaults ?? null}
        channels={channels}
        onCreated={(poll) => {
          setCreating(false);
          replace(poll);
          setParams({ id: poll.message_id }, { replace: true });
        }}
      />
    </>
  );
}
