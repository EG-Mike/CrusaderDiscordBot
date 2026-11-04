import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Archive, Database, Download, Eraser, HardDrive, KeyRound, Lock, RefreshCw, Shrink, SquareSlash, Trash2, Users } from "lucide-react";
import { ApiError, api } from "../lib/api";
import { useToast } from "../components/Toast";
import { Button, Dialog, SectionHeader, Skeleton, StatTile } from "../components/ui";
import BlizzardExplorer from "../components/BlizzardExplorer";

interface Overview {
  database: { path: string; size: number; wal_size: number; storage_backend: string };
  tables: { table: string; label: string; rows: number | null }[];
  stores: { name: string; records: number }[];
  audit_file_size: number;
  sessions: number;
  caches: { store: string; label: string; help: string; records: number | null }[];
  can_run: boolean;
}

interface Tool {
  action: string;
  title: string;
  description: ReactNode;
  icon: ReactNode;
  button: string;
  confirm?: string;
  body?: Record<string, string>;
}

function bytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

const TOOLS: { group: string; tools: Tool[] }[] = [
  {
    group: "Clean-up",
    tools: [
      {
        action: "apply-retention",
        title: "Apply log retention now",
        description: "Deletes audit and system log entries older than their retention settings, and expired dashboard logins. This also happens automatically every hour.",
        icon: <Eraser className="h-4 w-4" />,
        button: "Clean up",
        confirm: "Old audit and system log entries are deleted for good.",
      },
      {
        action: "compact",
        title: "Compact the database",
        description: "Gives back disk space left over from deleted entries. Takes a moment on a large database; the bot keeps running.",
        icon: <Shrink className="h-4 w-4" />,
        button: "Compact",
      },
      {
        action: "logout-others",
        title: "Log out everyone else",
        description: "Ends every other dashboard login, e.g. after someone lost their moderator role or used a shared computer. You stay logged in.",
        icon: <Users className="h-4 w-4" />,
        button: "Log out others",
        confirm: "Everyone else is logged out of the dashboard and has to log in again.",
      },
    ],
  },
  {
    group: "Discord",
    tools: [
      {
        action: "sync-commands",
        title: "Re-sync slash commands",
        description: "Registers the bot's slash commands in the server again, if commands are missing or out of date. This also happens at every start.",
        icon: <SquareSlash className="h-4 w-4" />,
        button: "Re-sync",
      },
      {
        action: "legacy-buttons",
        title: "Remove old Edit buttons from announcements",
        description: "Announcements published by very early versions carry a public Edit button. This removes it from all of them; safe to run more than once.",
        icon: <RefreshCw className="h-4 w-4" />,
        button: "Remove buttons",
      },
    ],
  },
];

function ToolRow({ tool, canRun, onRun, busy, result }: { tool: Tool; canRun: boolean; onRun: (t: Tool) => void; busy: boolean; result?: string }) {
  return (
    <li className="flex flex-wrap items-start gap-3 px-4 py-3.5 md:px-5">
      <div className="mt-0.5 text-faint">{tool.icon}</div>
      <div className="min-w-0 flex-1">
        <div className="font-medium">{tool.title}</div>
        <p className="mt-0.5 text-muted">{tool.description}</p>
        {result && <p className="mt-1 text-xs text-ok">{result}</p>}
      </div>
      <Button size="sm" onClick={() => onRun(tool)} loading={busy} disabled={!canRun} className="shrink-0">
        {tool.button}
      </Button>
    </li>
  );
}

export default function MaintenancePage() {
  const toast = useToast();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState<Tool | null>(null);

  const load = useCallback(() => {
    api.get<Overview>("/api/maintenance").then(setData).catch((e) => setError(e instanceof ApiError ? e.message : "Couldn't load"));
  }, []);
  useEffect(load, [load]);

  const run = async (tool: Tool) => {
    setConfirming(null);
    const key = tool.action + (tool.body?.store ?? "");
    setBusy(key);
    try {
      const { result } = await api.post<{ result: string }>(`/api/maintenance/${tool.action}`, tool.body ?? {});
      setResults((r) => ({ ...r, [key]: result }));
      toast("ok", result);
      load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That didn't work");
    } finally {
      setBusy(null);
    }
  };
  const request = (tool: Tool) => (tool.confirm ? setConfirming(tool) : run(tool));

  const canRun = data?.can_run ?? false;
  const recordsTotal = data?.stores.reduce((n, s) => n + s.records, 0) ?? 0;

  return (
    <>
      <SectionHeader eyebrow="System" title="Maintenance" description="Storage, backups and housekeeping. Everything here is recorded in the audit log." />

      {data && !canRun && (
        <div className="mb-5 flex items-center gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-muted">
          <Lock className="h-4 w-4 shrink-0 text-faint" /> You can look around; running tools and downloading backups is for admins.
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {data ? (
          <>
            <StatTile value={bytes(data.database.size + data.database.wal_size)} label="Database size" icon={<Database className="h-3 w-3" />} />
            <StatTile value={recordsTotal.toLocaleString()} label="Stored records" icon={<HardDrive className="h-3 w-3" />} />
            <StatTile value={bytes(data.audit_file_size)} label="audit.log files" icon={<Archive className="h-3 w-3" />} />
            <StatTile value={data.sessions} label="Dashboard logins" icon={<KeyRound className="h-3 w-3" />} />
          </>
        ) : (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[76px]" />)
        )}
      </div>

      {error && <p className="mb-5 text-crimson">{error}</p>}

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-5">
          <section className="card">
            <header className="border-b border-line px-4 py-3 md:px-5">
              <div className="label">Backup</div>
            </header>
            <div className="flex flex-wrap items-start gap-3 px-4 py-3.5 md:px-5">
              <Download className="mt-0.5 h-4 w-4 text-faint" />
              <div className="min-w-0 flex-1">
                <div className="font-medium">Download a database backup</div>
                <p className="mt-0.5 text-muted">
                  A complete, consistent copy of the database, made while the bot keeps running. API keys inside it stay encrypted: restoring them
                  also needs the <span className="font-mono text-fg">secret.key</span> file from the server.
                </p>
              </div>
              {canRun ? (
                <a
                  href="/api/maintenance/backup"
                  download
                  className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-lg border border-gold/50 bg-gold/15 px-2.5 text-xs font-medium text-gold transition hover:bg-gold/25"
                >
                  <Download className="h-3.5 w-3.5" /> Download
                </a>
              ) : (
                <Button size="sm" disabled>
                  Download
                </Button>
              )}
            </div>
          </section>

          {TOOLS.map(({ group, tools }) => (
            <section key={group} className="card">
              <header className="border-b border-line px-4 py-3 md:px-5">
                <div className="label">{group}</div>
              </header>
              <ul className="divide-y divide-line">
                {tools.map((tool) => (
                  <ToolRow key={tool.action} tool={tool} canRun={canRun} onRun={request} busy={busy === tool.action} result={results[tool.action]} />
                ))}
              </ul>
            </section>
          ))}
        </div>

        <div className="space-y-5">
          <section className="card">
            <header className="border-b border-line px-4 py-3 md:px-5">
              <div className="label">API caches</div>
            </header>
            <ul className="divide-y divide-line">
              {data
                ? data.caches.map((c) => (
                    <ToolRow
                      key={c.store}
                      tool={{
                        action: "clear-cache",
                        body: { store: c.store },
                        title: `${c.label}${c.records !== null ? ` · ${c.records.toLocaleString()} entries` : ""}`,
                        description: `${c.help} Empty it if names or icons look wrong; they're looked up again when needed.`,
                        icon: <Trash2 className="h-4 w-4" />,
                        button: "Empty",
                        confirm: `${c.label} is emptied; the next lookups are slower while it refills.`,
                      }}
                      canRun={canRun && c.records !== null}
                      onRun={request}
                      busy={busy === `clear-cache${c.store}`}
                      result={results[`clear-cache${c.store}`]}
                    />
                  ))
                : <li className="p-4"><Skeleton className="h-12" /></li>}
            </ul>
          </section>

          <section className="card">
            <header className="flex items-center justify-between border-b border-line px-4 py-3 md:px-5">
              <div className="label">What's stored</div>
              {data && <span className="text-2xs text-faint">Storage: {data.database.storage_backend === "sqlite" ? "database" : "JSON files + database"}</span>}
            </header>
            {data ? (
              <table className="w-full text-[13px]">
                <tbody className="divide-y divide-line">
                  {data.tables.map((t) => (
                    <tr key={t.table}>
                      <td className="px-4 py-2 text-muted md:px-5">{t.label}</td>
                      <td className="px-4 py-2 text-right tabular-nums md:px-5">{t.rows === null ? "—" : t.rows.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="p-4">
                <Skeleton className="h-40" />
              </div>
            )}
            {data && <p className="truncate border-t border-line px-4 py-2 font-mono text-2xs text-faint md:px-5" title={data.database.path}>{data.database.path}</p>}
          </section>
        </div>
      </div>

      <div className="mt-5">
        <BlizzardExplorer />
      </div>

      <Dialog
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        title={`${confirming?.title ?? ""}?`}
        actions={
          <>
            <Button variant="ghost" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => confirming && run(confirming)}>
              {confirming?.button}
            </Button>
          </>
        }
      >
        <p className="text-muted">{confirming?.confirm}</p>
      </Dialog>
    </>
  );
}
