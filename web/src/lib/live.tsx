// Live bot status + event stream over /api/ws, shared app-wide.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Status } from "./api";

type Listener = (event: { type: string; [k: string]: unknown }) => void;

interface LiveValue {
  status: Status | null;
  connected: boolean;
  subscribe: (fn: Listener) => () => void;
  refreshStatus: () => void;
}

const LiveContext = createContext<LiveValue | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [connected, setConnected] = useState(false);
  const listeners = useRef(new Set<Listener>());

  const refreshStatus = () => {
    api.get<Status>("/api/status").then(setStatus).catch(() => undefined);
  };

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
      ws.onopen = () => setConnected(true);
      ws.onmessage = (msg) => {
        const event = JSON.parse(msg.data);
        if (event.type === "status") setStatus(event.data);
        if (event.type === "settings.changed") refreshStatus();
        listeners.current.forEach((fn) => fn(event));
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 3000);
      };
    };
    refreshStatus();
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, []);

  const subscribe = (fn: Listener) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  };

  return (
    <LiveContext.Provider value={{ status, connected, subscribe, refreshStatus }}>{children}</LiveContext.Provider>
  );
}

export function useLive() {
  const value = useContext(LiveContext);
  if (!value) throw new Error("useLive outside LiveProvider");
  return value;
}

export function formatDuration(seconds: number) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
