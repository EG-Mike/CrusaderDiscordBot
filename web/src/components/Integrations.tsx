// API keys for external services. Write-only: a saved key is never sent
// back to the browser, only whether one is set.
import { useEffect, useState } from "react";
import { CheckCircle2, KeyRound, PlugZap, XCircle } from "lucide-react";
import { api, type SecretField, type Service } from "../lib/api";
import { useToast } from "./Toast";
import { Badge, Button, Skeleton, cx } from "./ui";

function ServiceCard({ service, onChange }: { service: Service; onChange: () => void }) {
  const toast = useToast();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [idField, secretField] = service.fields;
  const dirty = Object.values(values).some((v) => v.trim());

  const test = async () => {
    setBusy("test");
    setResult(null);
    try {
      setResult(
        await api.post<{ ok: boolean; message: string }>(`/api/integrations/${service.id}/test`, {
          client_id: values[idField.key] ?? "",
          client_secret: values[secretField.key] ?? "",
        }),
      );
    } catch (e) {
      setResult({ ok: false, message: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy("save");
    try {
      for (const [key, value] of Object.entries(values)) {
        if (value.trim()) await api.put(`/api/integrations/${key}`, { value });
      }
      setValues({});
      toast("ok", `${service.label} keys saved. They're used straight away.`);
      onChange();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Remove the saved ${service.label} keys?`)) return;
    for (const field of service.fields) await api.del(`/api/integrations/${field.key}`).catch(() => undefined);
    setResult(null);
    onChange();
  };

  const field = (f: SecretField) => (
    <label key={f.key} className="block">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className="font-medium">{f.label}</span>
        {f.source === "dashboard" && <Badge tone="ok">Saved</Badge>}
        {f.source === "env" && <Badge tone="info">From .env</Badge>}
      </div>
      <input
        type="password"
        autoComplete="off"
        spellCheck={false}
        className="input font-mono"
        placeholder={f.set ? "•••••••••••• (saved, type to replace)" : "Not set"}
        value={values[f.key] ?? ""}
        onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
      />
      <p className="mt-1 text-xs text-faint">{f.help}</p>
    </label>
  );

  return (
    <div className="card p-4 md:p-5">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <KeyRound className="h-4 w-4 text-gold" />
        <h3 className="font-semibold">{service.label}</h3>
        {service.required ? <Badge tone="gold">Required</Badge> : <Badge>Optional</Badge>}
        {service.configured ? <Badge tone="ok">Connected</Badge> : <Badge tone="warn">Not set up</Badge>}
      </div>
      <div className="grid gap-4 md:grid-cols-2">{service.fields.map(field)}</div>
      {result && (
        <div
          className={cx(
            "mt-4 flex items-center gap-2 rounded-lg border px-3 py-2",
            result.ok ? "border-ok/30 bg-ok/10 text-ok" : "border-crimson/30 bg-crimson/10 text-crimson",
          )}
        >
          {result.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {result.message}
        </div>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <Button
          size="sm"
          icon={<PlugZap className="h-3.5 w-3.5" />}
          loading={busy === "test"}
          disabled={!dirty && !service.configured}
          onClick={test}
        >
          Test connection
        </Button>
        <span className="flex-1" />
        {service.fields.some((f) => f.source === "dashboard") && (
          <Button size="sm" variant="ghost" onClick={remove}>
            Remove
          </Button>
        )}
        <Button size="sm" variant="primary" loading={busy === "save"} disabled={!dirty} onClick={save}>
          Save keys
        </Button>
      </div>
    </div>
  );
}

export function Integrations({ only }: { only?: string[] }) {
  const [services, setServices] = useState<Service[] | null>(null);
  const load = () => api.get<Service[]>("/api/integrations").then(setServices).catch(() => setServices([]));
  useEffect(() => {
    load();
  }, []);
  if (!services) return <Skeleton className="h-48" />;
  return (
    <div className="space-y-3">
      {services
        .filter((s) => !only || only.includes(s.id))
        .map((s) => (
          <ServiceCard key={s.id} service={s} onChange={load} />
        ))}
    </div>
  );
}
