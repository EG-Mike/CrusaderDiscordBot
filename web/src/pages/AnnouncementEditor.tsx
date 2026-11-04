import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  History,
  LayoutTemplate,
  Loader2,
  RotateCcw,
  Rocket,
  Trash2,
} from "lucide-react";
import {
  ApiError,
  api,
  type Announcement,
  type AnnouncementTemplate,
  type AnnouncementVersion,
  type Channel,
} from "../lib/api";
import { useGuildLookups } from "../lib/guild";
import { useLive } from "../lib/live";
import { fullDateTime, timeAgo, useNow } from "../lib/time";
import { ContainerPreview, MessageFrame } from "../components/DiscordPreview";
import { ColorPicker, MarkdownEditor } from "../components/MarkdownEditor";
import { useToast } from "../components/Toast";
import { Badge, Button, Dialog, EmptyState, Skeleton } from "../components/ui";
import { who } from "./Announcements";

const MAX_TOTAL = 3980;
const AUTOSAVE_MS = 800;

interface Fields {
  title: string;
  body: string;
  color: string | null;
  ping: string | null;
}

const fieldsOf = (a: Announcement): Fields => ({ title: a.title ?? "", body: a.body, color: a.color, ping: a.ping });
const sameFields = (a: Fields, b: Fields) => a.title === b.title && a.body === b.body && a.color === b.color && a.ping === b.ping;

type SaveState = "saved" | "dirty" | "saving" | "error";

function pingLabel(ping: string | null, roles: { id: string; name: string }[]) {
  if (!ping) return "No ping";
  if (ping === "everyone" || ping === "here") return `@${ping}`;
  return `@${roles.find((r) => `role:${r.id}` === ping)?.name ?? "role"}`;
}

/** Pick who gets notified: nobody, @everyone, @here or one role. */
function PingPicker({ value, onChange, roles, disabled }: { value: string | null; onChange: (v: string | null) => void; roles: { id: string; name: string; managed: boolean }[]; disabled?: boolean }) {
  return (
    <select className="input" value={value ?? ""} disabled={disabled} onChange={(e) => onChange(e.target.value || null)} aria-label="Ping">
      <option value="">Nobody (no ping)</option>
      <option value="here">@here (members who are online)</option>
      <option value="everyone">@everyone</option>
      <optgroup label="A role">
        {roles
          .filter((r) => !r.managed)
          .map((r) => (
            <option key={r.id} value={`role:${r.id}`}>
              @{r.name}
            </option>
          ))}
      </optgroup>
    </select>
  );
}

function PublishDialog({
  open,
  onClose,
  onPublish,
  channels,
  ping,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onPublish: (channelId: string) => void;
  channels: Channel[];
  ping: string;
  busy: boolean;
}) {
  const usable = channels.filter((c) => c.type === "text" || c.type === "news");
  const [channelId, setChannelId] = useState("");
  const chosen = usable.find((c) => c.id === channelId);
  const byCategory = useMemo(() => {
    const groups = new Map<string, Channel[]>();
    usable.forEach((c) => groups.set(c.category ?? "No category", [...(groups.get(c.category ?? "No category") ?? []), c]));
    return [...groups.entries()];
  }, [usable]);
  return (
    <Dialog
      open={open}
      onClose={onClose}
      busy={busy}
      title="Publish announcement"
      actions={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" icon={<Rocket className="h-4 w-4" />} onClick={() => onPublish(channelId)} disabled={!chosen || !chosen.bot_can_send} loading={busy}>
            {chosen ? `Publish to #${chosen.name}` : "Publish"}
          </Button>
        </>
      }
    >
      <label className="label mb-1.5 block" htmlFor="publish-channel">
        Channel
      </label>
      <select id="publish-channel" className="input" value={channelId} onChange={(e) => setChannelId(e.target.value)} autoFocus>
        <option value="">Choose a channel…</option>
        {byCategory.map(([category, list]) => (
          <optgroup key={category} label={category}>
            {list.map((c) => (
              <option key={c.id} value={c.id} disabled={!c.bot_can_send}>
                #{c.name}
                {c.type === "news" ? " (announcement channel)" : ""}
                {!c.bot_can_send ? " (bot can't post here)" : ""}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      <p className="mt-3 text-muted">
        {ping === "No ping" ? (
          "Nobody will be pinged."
        ) : (
          <>
            This will ping <span className="font-medium text-fg">{ping}</span>.
          </>
        )}{" "}
        You can still edit the text afterwards; edits don't ping again.
      </p>
    </Dialog>
  );
}

function VersionsPanel({ versions, onRestore }: { versions: AnnouncementVersion[]; onRestore: (v: AnnouncementVersion) => void }) {
  if (!versions.length) return <p className="px-1 py-2 text-muted">No published versions yet. Each publish and edit in Discord is kept here.</p>;
  return (
    <ul className="divide-y divide-line">
      {versions.map((v) => (
        <li key={v.id} className="flex items-center gap-3 py-2.5">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Badge tone={v.event === "published" ? "ok" : "info"}>{v.event === "published" ? "Published" : "Edited"}</Badge>
              <span className="truncate text-muted">{v.title || "Untitled"}</span>
            </div>
            <div className="mt-0.5 text-2xs text-faint" title={fullDateTime(v.saved_at)}>
              {fullDateTime(v.saved_at)} · {who(v.saved_by)}
            </div>
          </div>
          <Button size="sm" variant="ghost" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => onRestore(v)}>
            Load
          </Button>
        </li>
      ))}
    </ul>
  );
}

export default function AnnouncementEditor() {
  const { id } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const now = useNow();
  const { subscribe, status } = useLive();
  const { roles, channels } = useGuildLookups();

  const [record, setRecord] = useState<Announcement | null>(null);
  const [versions, setVersions] = useState<AnnouncementVersion[]>([]);
  const [fields, setFields] = useState<Fields | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<Announcement | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<null | "publish" | "delete" | "template" | "history">(null);
  const [templateName, setTemplateName] = useState("");

  // Refs so the autosave timer always sees the latest values.
  const recordRef = useRef<Announcement | null>(null);
  const fieldsRef = useRef<Fields | null>(null);
  recordRef.current = record;
  fieldsRef.current = fields;
  const saving = useRef(false);

  const load = useCallback(() => {
    api
      .get<{ announcement: Announcement; versions: AnnouncementVersion[] }>(`/api/announcements/${id}`)
      .then(({ announcement, versions }) => {
        setRecord(announcement);
        setVersions(versions);
        setFields(fieldsOf(announcement));
        setSaveState("saved");
        setConflict(null);
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : "Couldn't load this announcement"));
  }, [id]);
  useEffect(load, [load]);

  const isDraft = record?.status === "draft";
  const dirty = !!record && !!fields && !sameFields(fields, fieldsOf(record));

  // --- saving ---------------------------------------------------------------

  const saveDraft = useCallback(async () => {
    const current = recordRef.current;
    const latest = fieldsRef.current;
    if (!current || !latest || saving.current || current.status !== "draft") return;
    if (sameFields(latest, fieldsOf(current))) {
      setSaveState("saved");
      return;
    }
    saving.current = true;
    setSaveState("saving");
    try {
      const saved = await api.put<Announcement>(`/api/announcements/${current.id}`, { ...latest, revision: current.revision });
      setRecord(saved);
      setSaveError(null);
      // Typed more while saving? Save again.
      setSaveState(fieldsRef.current && !sameFields(fieldsRef.current, fieldsOf(saved)) ? "dirty" : "saved");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const latest = await api.get<{ announcement: Announcement }>(`/api/announcements/${current.id}`).catch(() => null);
        setConflict(latest?.announcement ?? current);
        setSaveState("error");
        setSaveError("Someone else changed this draft.");
      } else {
        setSaveState("error");
        setSaveError(e instanceof ApiError ? e.message : "Couldn't save");
      }
    } finally {
      saving.current = false;
    }
  }, []);

  useEffect(() => {
    if (!isDraft || !dirty || conflict) return;
    setSaveState((s) => (s === "saving" ? s : "dirty"));
    const t = window.setTimeout(saveDraft, AUTOSAVE_MS);
    return () => window.clearTimeout(t);
  }, [fields, isDraft, dirty, conflict, saveDraft, saveState]);

  // Warn before leaving with unsaved work.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // Someone else changed it: reload if we have no local changes, otherwise warn.
  useEffect(
    () =>
      subscribe((event) => {
        if (event.type !== "announcements.changed" || String(event.id) !== String(id)) return;
        if (event.deleted) {
          toast("error", "This announcement was deleted by someone else.");
          navigate("/announcements");
          return;
        }
        if (saving.current) return;
        api.get<{ announcement: Announcement; versions: AnnouncementVersion[] }>(`/api/announcements/${id}`).then(({ announcement, versions }) => {
          const mine = recordRef.current;
          if (mine && announcement.revision === mine.revision) return; // our own save
          setVersions(versions);
          const local = fieldsRef.current;
          if (!mine || !local || sameFields(local, fieldsOf(mine))) {
            setRecord(announcement);
            setFields(fieldsOf(announcement));
          } else setConflict(announcement);
        });
      }),
    [subscribe, id, navigate, toast],
  );

  const update = (patch: Partial<Fields>) => setFields((f) => (f ? { ...f, ...patch } : f));

  const keepMine = async () => {
    if (!conflict || !fields) return;
    // Retry our save on top of their revision.
    setRecord({ ...conflict });
    recordRef.current = conflict;
    setConflict(null);
    setSaveError(null);
    if (conflict.status === "draft") window.setTimeout(saveDraft, 0);
  };

  const takeTheirs = () => {
    if (!conflict) return;
    setRecord(conflict);
    setFields(fieldsOf(conflict));
    setConflict(null);
    setSaveError(null);
    setSaveState("saved");
  };

  // --- actions --------------------------------------------------------------

  const publish = async (channelId: string) => {
    if (!record || !fields) return;
    setPublishing(true);
    try {
      // Make sure the latest text is saved first.
      let current = record;
      if (!sameFields(fields, fieldsOf(record))) current = await api.put<Announcement>(`/api/announcements/${record.id}`, { ...fields, revision: record.revision });
      const published = await api.post<Announcement>(`/api/announcements/${record.id}/publish`, { channel_id: channelId, revision: current.revision });
      setRecord(published);
      setFields(fieldsOf(published));
      setDialog(null);
      toast("ok", `Published to #${published.channel_name ?? "the channel"}`);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't publish");
    } finally {
      setPublishing(false);
    }
  };

  const updatePublished = async () => {
    if (!record || !fields) return;
    setBusy(true);
    try {
      const saved = await api.put<Announcement>(`/api/announcements/${record.id}`, { title: fields.title, body: fields.body, color: fields.color, revision: record.revision });
      setRecord(saved);
      setFields(fieldsOf(saved));
      toast("ok", "Updated in Discord");
      load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) load();
      toast("error", e instanceof ApiError ? e.message : "Couldn't update the message");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!record) return;
    setBusy(true);
    try {
      const result = await api.del<{ notes: string[] }>(`/api/announcements/${record.id}`);
      toast(result.notes.length ? "error" : "ok", result.notes.length ? `Deleted. ${result.notes.join(" ")}` : "Deleted");
      navigate(`/announcements${record.status === "published" ? "?tab=published" : ""}`);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't delete");
      setBusy(false);
    }
  };

  const duplicate = async () => {
    if (!record) return;
    try {
      const copy = await api.post<Announcement>(`/api/announcements/${record.id}/duplicate`);
      toast("ok", "Copied into a new draft");
      navigate(`/announcements/${copy.id}`);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't copy");
    }
  };

  const saveTemplate = async () => {
    if (!fields) return;
    setBusy(true);
    try {
      const template = await api.post<AnnouncementTemplate>("/api/announcement-templates", { name: templateName, ...fields });
      toast("ok", `Saved the template “${template.name}”`);
      setDialog(null);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't save the template");
    } finally {
      setBusy(false);
    }
  };

  // --- render ---------------------------------------------------------------

  if (loadError)
    return (
      <div className="card">
        <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't open this announcement">
          {loadError}{" "}
          <Link to="/announcements" className="text-gold hover:underline">
            Back to announcements
          </Link>
        </EmptyState>
      </div>
    );
  if (!record || !fields)
    return (
      <div className="grid gap-5 lg:grid-cols-2">
        <Skeleton className="h-[480px]" />
        <Skeleton className="h-[480px]" />
      </div>
    );

  const total = fields.title.length + fields.body.length;
  const tooLong = total > MAX_TOTAL;
  const empty = !fields.body.trim();
  const ctx = { roles, channels };
  const discordUrl = record.message_id && status?.guild ? `https://discord.com/channels/${status.guild.id}/${record.channel_id}/${record.message_id}` : null;

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Link to={`/announcements${isDraft ? "" : "?tab=published"}`} className="inline-flex items-center gap-1.5 text-xs text-muted transition hover:text-fg">
          <ArrowLeft className="h-3.5 w-3.5" /> Announcements
        </Link>
        <Badge tone={isDraft ? "neutral" : "ok"}>{isDraft ? "Draft" : "Published"}</Badge>
        {isDraft ? (
          <span className="flex items-center gap-1.5 text-xs text-faint" aria-live="polite">
            {saveState === "saving" && (
              <>
                <Loader2 className="h-3 w-3 animate-spin" /> Saving…
              </>
            )}
            {saveState === "saved" && (
              <>
                <Check className="h-3 w-3 text-ok" /> Saved {timeAgo(record.updated_at, now)}
              </>
            )}
            {saveState === "dirty" && "Unsaved changes"}
            {saveState === "error" && <span className="text-crimson">{saveError}</span>}
          </span>
        ) : (
          <span className="text-xs text-faint">
            Published {record.channel_name ? `in #${record.channel_name} ` : ""}by {who(record.published_by)}, {record.published_at ? timeAgo(record.published_at, now) : ""}
          </span>
        )}
        <div className="ml-auto flex flex-wrap gap-2">
          {!isDraft && (
            <Button size="sm" variant="ghost" icon={<History className="h-3.5 w-3.5" />} onClick={() => setDialog("history")}>
              History · {versions.length}
            </Button>
          )}
          <Button size="sm" variant="ghost" icon={<LayoutTemplate className="h-3.5 w-3.5" />} onClick={() => { setTemplateName(fields.title); setDialog("template"); }}>
            Save as template
          </Button>
          {!isDraft && (
            <Button size="sm" variant="ghost" icon={<Copy className="h-3.5 w-3.5" />} onClick={duplicate}>
              Copy to new draft
            </Button>
          )}
          <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => setDialog("delete")}>
            Delete
          </Button>
        </div>
      </div>

      {conflict && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1 text-muted">
            <span className="font-medium text-warn">{who(conflict.updated_by)} changed this announcement</span> while you were editing.
          </span>
          <Button size="sm" variant="outline" onClick={takeTheirs}>
            Use their version
          </Button>
          <Button size="sm" variant="outline" onClick={keepMine}>
            Keep mine
          </Button>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <section className="card min-w-0 space-y-4 p-4 md:p-5">
          <div>
            <label className="label mb-1.5 block" htmlFor="ann-title">
              Title <span className="normal-case tracking-normal text-faint">(optional, shown as a heading)</span>
            </label>
            <input
              id="ann-title"
              className="input"
              value={fields.title}
              maxLength={256}
              placeholder="e.g. Raid schedule for next week"
              onChange={(e) => update({ title: e.target.value })}
            />
          </div>
          <div>
            <div className="mb-1.5 flex items-baseline justify-between gap-2">
              <span className="label">Text</span>
              <span className="text-2xs text-faint">A line with just --- starts a new section</span>
            </div>
            <MarkdownEditor
              label="Announcement text"
              value={fields.body}
              onChange={(body) => update({ body })}
              limit={MAX_TOTAL - fields.title.length}
              roles={roles}
              channels={channels}
              sections
              rows={16}
              placeholder={"## Big header 🎉\nWrite as you would in Discord: **bold**, *italic*, lists, links…"}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <span className="label mb-1.5 block">Accent colour</span>
              <ColorPicker value={fields.color} fallback="#5865F2" onChange={(color) => update({ color })} />
            </div>
            <div>
              <span className="label mb-1.5 block">Ping</span>
              {isDraft ? (
                <PingPicker value={fields.ping} roles={roles} onChange={(ping) => update({ ping })} />
              ) : (
                <p className="py-2 text-muted">{pingLabel(record.ping, roles)} <span className="text-faint">(fixed once published)</span></p>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
            {tooLong && <span className="mr-auto text-xs text-crimson">Too long for one Discord message: shorten it by {total - MAX_TOTAL} characters.</span>}
            {isDraft ? (
              <Button variant="primary" icon={<Rocket className="h-4 w-4" />} onClick={() => setDialog("publish")} disabled={empty || tooLong || !!conflict}>
                Publish…
              </Button>
            ) : (
              <>
                {dirty && (
                  <Button variant="ghost" onClick={() => setFields(fieldsOf(record))} disabled={busy}>
                    Discard changes
                  </Button>
                )}
                <Button variant="primary" onClick={updatePublished} disabled={!dirty || empty || tooLong} loading={busy}>
                  Update in Discord
                </Button>
              </>
            )}
          </div>
        </section>

        <section className="min-w-0 lg:sticky lg:top-4 lg:self-start">
          <div className="mb-2 flex items-center justify-between">
            <span className="label">Preview</span>
            {discordUrl && (
              <a href={discordUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-gold hover:underline">
                Open in Discord <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
          <MessageFrame>
            <ContainerPreview title={fields.title.trim() || null} body={fields.body} color={fields.color} ping={isDraft ? pingTextFor(fields.ping) : pingTextFor(record.ping)} ctx={ctx} />
          </MessageFrame>
          <p className="mt-2 text-2xs text-faint">
            Close to what Discord shows; fonts and spacing differ slightly by device.
            {!isDraft && dirty && <span className="text-warn"> These changes aren't in Discord yet.</span>}
          </p>
        </section>
      </div>

      <PublishDialog
        open={dialog === "publish"}
        onClose={() => setDialog(null)}
        onPublish={publish}
        channels={channels}
        ping={pingLabel(fields.ping, roles)}
        busy={publishing}
      />

      <Dialog
        open={dialog === "delete"}
        onClose={() => setDialog(null)}
        busy={busy}
        title={isDraft ? "Delete draft?" : "Delete announcement?"}
        actions={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove} loading={busy}>
              {isDraft ? "Delete draft" : "Delete from Discord too"}
            </Button>
          </>
        }
      >
        <p className="text-muted">
          {isDraft
            ? "The draft is removed for everyone (and its /announce draft message, if it has one)."
            : `The message in #${record.channel_name ?? "its channel"} is deleted for everyone, along with its version history. This can't be undone.`}
        </p>
      </Dialog>

      <Dialog
        open={dialog === "template"}
        onClose={() => setDialog(null)}
        busy={busy}
        title="Save as template"
        actions={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={saveTemplate} loading={busy} disabled={!templateName.trim()}>
              Save template
            </Button>
          </>
        }
      >
        <label className="label mb-1.5 block" htmlFor="template-name">
          Template name
        </label>
        <input id="template-name" className="input" value={templateName} maxLength={80} autoFocus onChange={(e) => setTemplateName(e.target.value)} placeholder="e.g. Weekly raid schedule" />
        <p className="mt-2 text-muted">Saves the title, text, colour and ping as a starting point for new announcements.</p>
      </Dialog>

      <Dialog open={dialog === "history"} onClose={() => setDialog(null)} title="Version history" wide>
        <VersionsPanel
          versions={versions}
          onRestore={(v) => {
            update({ title: v.title ?? "", body: v.body, color: v.color });
            setDialog(null);
            toast("ok", "Loaded into the editor. Click Update in Discord to apply it.");
          }}
        />
      </Dialog>
    </>
  );
}

function pingTextFor(ping: string | null) {
  if (!ping) return null;
  if (ping === "everyone" || ping === "here") return `@${ping}`;
  return `<@&${ping.split(":")[1]}>`;
}
