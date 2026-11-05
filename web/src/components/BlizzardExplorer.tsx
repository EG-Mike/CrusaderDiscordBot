// Maintenance page: send one raw request to Blizzard's API and see what it
// answers. For finding out what WoW Forever's API offers while Blizzard
// hasn't documented it (webapi/game_api.py, docs/wow-forever-api.md).
import { useEffect, useState, type FormEvent } from "react";
import { Check, ClipboardCopy, Lock, Send } from "lucide-react";
import { ApiError, api, type Json } from "../lib/api";
import { Badge, Button, cx } from "./ui";

interface Defaults {
  region: string;
  regions: string[];
  namespaces: Record<"static" | "dynamic" | "profile", string>;
  game: string;
  configured: boolean;
  can_explore: boolean;
}

interface Answer {
  status: number;
  meaning: string;
  ms: number;
  headers: Record<string, string>;
  body: Json | null;
  truncated?: string;
}

interface HistoryItem {
  path: string;
  namespace: string;
  region: string;
  status: number;
}

type Kind = "static" | "dynamic" | "profile" | "custom";

// Starting points: what the bot uses, and what to check once Blizzard opens more.
const PRESETS: { label: string; kind: Exclude<Kind, "custom">; path: string }[] = [
  { label: "Rulesets", kind: "dynamic", path: "/data/wow/ruleset/index" },
  { label: "Region", kind: "dynamic", path: "/data/wow/region/index" },
  { label: "Auction houses", kind: "dynamic", path: "/data/wow/ruleset/2/auctions/index" },
  { label: "Classes", kind: "static", path: "/data/wow/playable-class/index" },
  { label: "Specs", kind: "static", path: "/data/wow/playable-specialization/index" },
  { label: "Races", kind: "static", path: "/data/wow/playable-race/index" },
  { label: "Professions", kind: "static", path: "/data/wow/profession/index" },
  { label: "Raids (journal)", kind: "static", path: "/data/wow/journal-instance/index" },
  { label: "Achievements", kind: "static", path: "/data/wow/achievement/index" },
  { label: "Character", kind: "profile", path: "/profile/wow/character/first-surname" },
  { label: "Guild", kind: "profile", path: "/data/wow/guild/guild-name" },
];

const tone = (status: number) => (status === 200 ? "ok" : status === 404 ? "warn" : "crimson");

export default function BlizzardExplorer() {
  const [defaults, setDefaults] = useState<Defaults | null>(null);
  const [region, setRegion] = useState("");
  const [kind, setKind] = useState<Kind>("dynamic");
  const [custom, setCustom] = useState("");
  const [path, setPath] = useState("/data/wow/ruleset/index");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.get<Defaults>("/api/game/explorer").then((d) => {
      setDefaults(d);
      setRegion(d.region);
    }).catch(() => setFailure("Couldn't load the explorer settings."));
  }, []);

  // The configured namespace family, moved to the chosen region ("static-forever-eu" -> "static-forever-us").
  const namespaceFor = (k: Kind) => {
    if (k === "custom" || !defaults) return custom.trim();
    return defaults.namespaces[k].replace(/-[a-z]{2}$/, `-${region}`);
  };
  const namespace = namespaceFor(kind);

  const send = async (e?: FormEvent, override?: HistoryItem) => {
    e?.preventDefault();
    const req = override ?? { path, namespace, region, status: 0 };
    setBusy(true);
    setFailure(null);
    try {
      const res = await api.post<Answer>("/api/game/explore", { path: req.path, namespace: req.namespace, region: req.region });
      setAnswer(res);
      setHistory((h) => [{ ...req, status: res.status }, ...h.filter((x) => x.path !== req.path || x.namespace !== req.namespace || x.region !== req.region)].slice(0, 8));
    } catch (err) {
      setAnswer(null);
      setFailure(err instanceof ApiError ? err.message : "That didn't work");
    } finally {
      setBusy(false);
    }
  };

  const restore = (item: HistoryItem) => {
    setPath(item.path);
    setRegion(item.region);
    setKind("custom");
    setCustom(item.namespace);
    void send(undefined, item);
  };

  const bodyText = answer?.body != null ? JSON.stringify(answer.body, null, 2) : "";
  const canExplore = defaults?.can_explore ?? false;

  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Blizzard API explorer</div>
        <p className="mt-1 text-muted">
          Send one request to Blizzard's API to see what it offers for {defaults?.game === "forever" ? "WoW Forever" : "this game"}. 403 means the namespace
          doesn't exist; 404 means it exists but has nothing at that path. Uses the bot's own API key, never a member's login.
        </p>
      </header>

      {defaults && !defaults.configured && (
        <p className="px-4 py-3 text-warn md:px-5">Blizzard API keys aren't set up yet (Settings, Integrations).</p>
      )}
      {defaults && !canExplore && (
        <p className="flex items-center gap-2 px-4 py-3 text-muted md:px-5">
          <Lock className="h-4 w-4 shrink-0 text-faint" /> The explorer is for admins.
        </p>
      )}

      <form onSubmit={send} className="space-y-3 px-4 py-3.5 md:px-5">
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((p) => (
            <button
              key={p.label}
              type="button"
              onClick={() => {
                setKind(p.kind);
                setPath(p.path);
              }}
              className={cx(
                "rounded-md border px-2 py-0.5 text-2xs transition",
                path === p.path && kind === p.kind ? "border-gold/50 bg-gold/10 text-gold" : "border-line text-muted hover:text-fg",
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="grid gap-2 sm:grid-cols-[90px_150px_minmax(0,1fr)]">
          <label className="block">
            <span className="label mb-1 block">Region</span>
            <select value={region} onChange={(e) => setRegion(e.target.value)} className="input w-full" disabled={!defaults}>
              {(defaults?.regions ?? []).map((r) => (
                <option key={r} value={r}>
                  {r.toUpperCase()}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="label mb-1 block">Namespace</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} className="input w-full">
              <option value="static">Static</option>
              <option value="dynamic">Dynamic</option>
              <option value="profile">Profile</option>
              <option value="custom">Other…</option>
            </select>
          </label>
          <label className="block min-w-0">
            <span className="label mb-1 block">Path</span>
            <input value={path} onChange={(e) => setPath(e.target.value)} className="input w-full font-mono text-xs" spellCheck={false} placeholder="/data/wow/realm/index" />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {kind === "custom" ? (
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              className="input min-w-0 flex-1 font-mono text-xs"
              placeholder="static-1.60.1_70399-forever-eu"
              spellCheck={false}
              aria-label="Namespace"
            />
          ) : (
            <span className="min-w-0 flex-1 truncate font-mono text-2xs text-faint">namespace={namespace}</span>
          )}
          <Button type="submit" size="sm" variant="primary" icon={<Send className="h-3.5 w-3.5" />} loading={busy} disabled={!canExplore || !defaults?.configured || !path.trim() || !namespace}>
            Send
          </Button>
        </div>
      </form>

      {failure && <p className="border-t border-line px-4 py-3 text-crimson md:px-5">{failure}</p>}

      {answer && (
        <div className="border-t border-line">
          <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 md:px-5">
            <Badge tone={tone(answer.status)}>{answer.status}</Badge>
            <span className="text-muted">{answer.meaning}</span>
            <span className="text-2xs text-faint">{answer.ms} ms</span>
            {bodyText && (
              <button
                type="button"
                className="ml-auto inline-flex items-center gap-1 text-2xs text-faint hover:text-fg"
                onClick={() =>
                  navigator.clipboard?.writeText(bodyText).then(() => {
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1200);
                  })
                }
              >
                {copied ? <Check className="h-3 w-3 text-ok" /> : <ClipboardCopy className="h-3 w-3" />} Copy JSON
              </button>
            )}
          </div>
          {Object.keys(answer.headers).length > 0 && (
            <dl className="grid gap-x-3 gap-y-0.5 px-4 pb-2.5 font-mono text-2xs sm:grid-cols-[auto_minmax(0,1fr)] md:px-5">
              {Object.entries(answer.headers).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-faint">{k}</dt>
                  <dd className="truncate text-muted">{v}</dd>
                </div>
              ))}
            </dl>
          )}
          {answer.truncated && <p className="px-4 pb-3 text-warn md:px-5">{answer.truncated}</p>}
          {bodyText && (
            <pre className="max-h-[420px] overflow-auto border-t border-line bg-bg px-4 py-3 font-mono text-2xs leading-relaxed text-muted md:px-5">{bodyText}</pre>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div className="border-t border-line px-4 py-2.5 md:px-5">
          <div className="label mb-1">Recent</div>
          <ul className="space-y-0.5">
            {history.map((h) => (
              <li key={`${h.region}${h.namespace}${h.path}`}>
                <button type="button" onClick={() => restore(h)} className="flex w-full min-w-0 items-center gap-2 text-left text-2xs hover:text-fg">
                  <Badge tone={tone(h.status)}>{h.status}</Badge>
                  <span className="truncate font-mono text-muted">{h.path}</span>
                  <span className="ml-auto shrink-0 font-mono text-faint">{h.namespace}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
