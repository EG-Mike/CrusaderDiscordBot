import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { cx } from "./ui";

interface Toast {
  id: number;
  tone: "ok" | "error";
  text: string;
}

const ToastContext = createContext<(tone: Toast["tone"], text: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, tone, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "error" ? 6000 : 3000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cx(
              "card pointer-events-auto flex items-start gap-2.5 px-3.5 py-3 shadow-2xl shadow-black/50",
              t.tone === "ok" ? "border-ok/30" : "border-crimson/40",
            )}
          >
            {t.tone === "ok" ? (
              <CheckCircle2 className="mt-px h-4 w-4 shrink-0 text-ok" />
            ) : (
              <AlertTriangle className="mt-px h-4 w-4 shrink-0 text-crimson" />
            )}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);
