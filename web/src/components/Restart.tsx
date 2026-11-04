// Restart the bot from the dashboard and wait for it to come back.
// Only offered when the server is set up to restart the process after it
// exits (WEB_RESTART_ENABLED) and the user is an admin.
import { useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";
import { api, type Me, type Status } from "../lib/api";
import { useLive } from "../lib/live";
import { useMe } from "../lib/me";
import { useToast } from "./Toast";
import { Button } from "./ui";

export function useRestart() {
  const toast = useToast();
  const [restarting, setRestarting] = useState(false);

  const restart = async () => {
    setRestarting(true);
    try {
      await api.post("/api/bot/restart");
    } catch (e) {
      toast("error", (e as Error).message);
      setRestarting(false);
      return false;
    }
    // Wait for the old process to go away, then for the new one to answer.
    await new Promise((r) => setTimeout(r, 3000));
    for (let i = 0; i < 90; i++) {
      try {
        const status = await api.get<Status>("/api/status");
        if (status.online) {
          setRestarting(false);
          toast("ok", "The bot restarted and is back online.");
          window.location.reload();
          return true;
        }
      } catch {
        /* still down */
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    setRestarting(false);
    toast("error", "The bot hasn't come back after 3 minutes. Check its logs on the server.");
    return false;
  };

  const overlay = restarting ? (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="card flex flex-col items-center gap-3 px-8 py-7 text-center">
        <Loader2 className="h-6 w-6 animate-spin text-gold" />
        <div className="font-semibold">Restarting the bot…</div>
        <p className="max-w-xs text-muted">This usually takes 10 to 30 seconds. The page reloads when it's back.</p>
      </div>
    </div>
  ) : null;

  return { restart, restarting, overlay };
}

export function canRestart(me: Me, status: Status | null) {
  return !!status?.restart_available && me.levels.includes("admin");
}

export function RestartButton({ label = "Restart now" }: { label?: string }) {
  const { status } = useLive();
  const me = useMe();
  const { restart, restarting, overlay } = useRestart();
  if (!canRestart(me, status)) return null;
  return (
    <>
      <Button
        size="sm"
        variant="primary"
        icon={<RotateCcw className="h-3.5 w-3.5" />}
        loading={restarting}
        onClick={() => window.confirm("Restart the bot now? It's offline for a few seconds.") && restart()}
      >
        {label}
      </Button>
      {overlay}
    </>
  );
}
