// Top of the Notifications settings group: where notifications go right
// now, anything wrong with that, and a test button.
import { useEffect, useState } from "react";
import { AlertTriangle, BellRing, Hash, Send } from "lucide-react";
import { ApiError, api } from "../lib/api";
import { useLive } from "../lib/live";
import { useToast } from "./Toast";
import { Button } from "./ui";

interface Destination {
  enabled: boolean;
  pending_restart?: boolean;
  channel_id?: string | null;
  channel_name?: string | null;
  source?: "notifications" | "moderator" | null;
  problem?: string | null;
}

export function NotificationsPanel() {
  const [info, setInfo] = useState<Destination | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const { subscribe } = useLive();

  const load = () => api.get<Destination>("/api/notifications").then(setInfo).catch(() => setInfo(null));
  useEffect(() => {
    load();
    return subscribe((e) => e.type === "settings.changed" && load());
  }, [subscribe]); // eslint-disable-line react-hooks/exhaustive-deps

  const test = async () => {
    setBusy(true);
    try {
      const { channel } = await api.post<{ channel: string }>("/api/notifications/test");
      toast("ok", `Test notification posted in #${channel}`);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Couldn't post the test notification");
    } finally {
      setBusy(false);
    }
  };

  if (!info) return null;
  return (
    <div className="card mb-4 flex flex-wrap items-center gap-3 p-4">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-gold/30 bg-gold/10 text-gold">
        <BellRing className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        {!info.enabled ? (
          <p className="text-muted">
            {info.pending_restart ? "Notifications are switched on but start after the next restart." : "Notifications are switched off (Features)."}
          </p>
        ) : (
          <>
            <p>
              Notifications go to{" "}
              {info.channel_name ? (
                <span className="inline-flex items-center font-medium">
                  <Hash className="h-3.5 w-3.5 text-faint" />
                  {info.channel_name}
                </span>
              ) : (
                <span className="text-muted">no channel yet</span>
              )}
              {info.source === "moderator" && <span className="text-muted"> (the moderator chat channel, since no notifications channel is set)</span>}
            </p>
            {info.problem && (
              <p className="mt-1 flex items-center gap-1.5 text-xs text-warn">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {info.problem}
              </p>
            )}
          </>
        )}
      </div>
      {info.enabled && (
        <Button size="sm" icon={<Send className="h-3.5 w-3.5" />} onClick={test} loading={busy} disabled={!!info.problem}>
          Send a test
        </Button>
      )}
    </div>
  );
}
