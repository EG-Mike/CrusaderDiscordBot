import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, FileText, Hash, LayoutTemplate, Megaphone, Plus, Search, Trash2 } from "lucide-react";
import { ApiError, api, type Announcement, type AnnouncementList, type AnnouncementTemplate } from "../lib/api";
import { useLive } from "../lib/live";
import { timeAgo, useNow } from "../lib/time";
import { useToast } from "../components/Toast";
import { Button, Dialog, EmptyState, SectionHeader, Segmented, Skeleton, cx } from "../components/ui";

type Tab = "draft" | "published" | "templates";

/** Markdown -> a one-line plain-text excerpt for lists. */
export function excerpt(body: string, length = 160) {
  const plain = body
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^\s*(#{1,3}|-#|>|[-*]|\d+\.)\s+/gm, "")
    .replace(/^---$/gm, " ")
    .replace(/<@&\d+>/g, "@role")
    .replace(/<#\d+>/g, "#channel")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[*_~`|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > length ? `${plain.slice(0, length - 1)}…` : plain;
}

/** "Mike (1234)" -> "Mike". */
export function who(label: string | null) {
  return label ? label.replace(/\s*\(\d+\)$/, "") : "someone";
}

function AnnouncementRow({ a, now }: { a: Announcement; now: number }) {
  return (
    <li>
      <Link to={`/announcements/${a.id}`} className="flex gap-3 px-4 py-3 transition hover:bg-raised/50 md:px-5">
        <span className="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: a.color ?? "#5865F2" }} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className={cx("truncate font-medium", !a.title && "italic text-muted")}>{a.title || "Untitled"}</span>
            {a.status === "published" && a.channel_name && (
              <span className="inline-flex shrink-0 items-center text-xs text-muted">
                <Hash className="h-3 w-3 text-faint" />
                {a.channel_name}
              </span>
            )}
          </div>
          <p className="mt-0.5 truncate text-muted">{excerpt(a.body) || <span className="italic text-faint">No text yet</span>}</p>
        </div>
        <div className="shrink-0 text-right text-2xs text-faint">
          <div title={a.updated_at}>{timeAgo(a.status === "published" && a.published_at ? a.published_at : a.updated_at, now)}</div>
          <div className="mt-0.5 hidden sm:block">
            {a.status === "published" ? `by ${who(a.published_by)}` : `edited by ${who(a.updated_by)}`}
          </div>
        </div>
      </Link>
    </li>
  );
}

export default function AnnouncementsPage() {
  const [params, setParams] = useSearchParams();
  const tab = (["draft", "published", "templates"].includes(params.get("tab") ?? "") ? params.get("tab") : "draft") as Tab;
  const [q, setQ] = useState("");
  const [data, setData] = useState<AnnouncementList | null>(null);
  const [templates, setTemplates] = useState<AnnouncementTemplate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<AnnouncementTemplate | null>(null);
  const navigate = useNavigate();
  const toast = useToast();
  const { subscribe } = useLive();
  const now = useNow();

  const load = useCallback(() => {
    api.get<AnnouncementList>("/api/announcements").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load announcements"));
    api.get<AnnouncementTemplate[]>("/api/announcement-templates").then(setTemplates).catch(() => setTemplates([]));
  }, []);
  useEffect(load, [load]);
  useEffect(() => subscribe((e) => e.type === "announcements.changed" && load()), [subscribe, load]);

  const create = async (templateId?: number) => {
    setCreating(true);
    try {
      const created = await api.post<Announcement>("/api/announcements", templateId ? { template_id: templateId } : {});
      navigate(`/announcements/${created.id}`);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't start a new announcement");
      setCreating(false);
    }
  };

  const removeTemplate = async () => {
    if (!deleting) return;
    try {
      await api.del(`/api/announcement-templates/${deleting.id}`);
      toast("ok", `Deleted the template “${deleting.name}”`);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't delete the template");
    }
    setDeleting(null);
  };

  const needle = q.trim().toLowerCase();
  const list = (data?.announcements ?? []).filter(
    (a) => a.status === tab && (!needle || `${a.title ?? ""} ${a.body}`.toLowerCase().includes(needle)),
  );
  const templateList = (templates ?? []).filter((t) => !needle || `${t.name} ${t.title ?? ""} ${t.body}`.toLowerCase().includes(needle));
  const disabled = data !== null && !data.feature_enabled;

  return (
    <>
      <SectionHeader
        eyebrow="Communication"
        title="Announcements"
        description="Write announcements with a live preview, save drafts, and publish or edit them in Discord. Drafts from /announce show up here too."
        actions={
          <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => create()} loading={creating} disabled={disabled}>
            New announcement
          </Button>
        }
      />

      {disabled && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
          <span className="min-w-0 flex-1 text-muted">
            <span className="font-medium text-warn">Announcements are switched off.</span> You can read what's here, but not write or
            publish until the feature is on.
          </span>
          <Link to="/settings?group=features#FEATURE_ANNOUNCEMENTS_ENABLED" className="text-xs text-gold hover:underline">
            Turn it on
          </Link>
        </div>
      )}

      <section className="card">
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3 md:px-5 md:py-4">
          <Segmented
            label="Show"
            value={tab}
            onChange={(t) => setParams(t === "draft" ? {} : { tab: t }, { replace: true })}
            options={[
              ["draft", `Drafts${data ? ` · ${data.counts.draft}` : ""}`],
              ["published", `Published${data ? ` · ${data.counts.published}` : ""}`],
              ["templates", `Templates${templates ? ` · ${templates.length}` : ""}`],
            ] as const}
          />
          <div className="relative min-w-[200px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
            <input className="input pl-9" placeholder="Search titles and text…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search announcements" />
          </div>
        </div>

        {error ? (
          <EmptyState icon={<AlertTriangle className="h-6 w-6" />} title="Couldn't load announcements">
            {error}
          </EmptyState>
        ) : !data || templates === null ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        ) : tab === "templates" ? (
          templateList.length === 0 ? (
            <EmptyState icon={<LayoutTemplate className="h-6 w-6" />} title={needle ? "No template matches" : "No templates yet"}>
              Open any announcement and choose <span className="text-fg">Save as template</span> to reuse its layout, e.g. for a weekly raid
              schedule.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-line">
              {templateList.map((t) => (
                <li key={t.id} className="flex flex-wrap items-center gap-3 px-4 py-3 md:px-5">
                  <LayoutTemplate className="h-4 w-4 shrink-0 text-faint" />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{t.name}</div>
                    <p className="truncate text-muted">{t.title ? `${t.title} · ` : ""}{excerpt(t.body, 120)}</p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" variant="primary" onClick={() => create(t.id)} disabled={disabled || creating}>
                      Use template
                    </Button>
                    <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => setDeleting(t)} disabled={disabled} aria-label={`Delete ${t.name}`} />
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : list.length === 0 ? (
          <EmptyState
            icon={tab === "draft" ? <FileText className="h-6 w-6" /> : <Megaphone className="h-6 w-6" />}
            title={needle ? "Nothing matches" : tab === "draft" ? "No drafts" : "Nothing published yet"}
          >
            {needle ? "Try other words." : tab === "draft" ? "Start a new announcement; it's saved as a draft while you write." : "Published announcements appear here, and can still be edited."}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-line">
            {list.map((a) => (
              <AnnouncementRow key={a.id} a={a} now={now} />
            ))}
          </ul>
        )}
      </section>

      <Dialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete template?"
        actions={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={removeTemplate}>
              Delete template
            </Button>
          </>
        }
      >
        <p className="text-muted">
          “{deleting?.name}” will be gone for everyone. Announcements made from it aren't affected.
        </p>
      </Dialog>
    </>
  );
}
