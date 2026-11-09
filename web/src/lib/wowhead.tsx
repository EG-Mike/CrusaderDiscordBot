// Wowhead links and tooltips on the dashboard. Every item or spell shown on
// a page should link to its Wowhead page: render it with <WowLink> (or a
// plain <a href> with a URL the server built, e.g. gear items). With the
// "Wowhead tooltips" setting on, Wowhead's tooltip script is loaded once and
// re-checks the page whenever its content changes, so hovering any such link
// shows Wowhead's tooltip, on every page, including new ones.
import { useEffect, type ReactNode } from "react";
import { useLive } from "./live";

declare global {
  interface Window {
    whTooltips?: { colorLinks: boolean; iconizeLinks: boolean; renameLinks: boolean };
    $WowheadPower?: { refreshLinks?: () => void };
  }
}

const SCRIPT_URL = "https://wow.zamimg.com/js/tooltips.js";
export type WowKind = "item" | "spell";

/** The page of an item or spell from a link setting ("https://...{id}"), or null. */
export function wowheadUrl(template: string | null | undefined, id: number | null | undefined) {
  if (!template || !id) return null;
  return template.replace("{id}", String(id));
}

/** Loads Wowhead's tooltip script (once) while `enabled`, and refreshes links when the page changes. */
export function useWowheadTooltips(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    // Our own colours, icons and names stay: Wowhead only adds the hover tooltip.
    window.whTooltips = { colorLinks: false, iconizeLinks: false, renameLinks: false };
    if (!document.querySelector(`script[src="${SCRIPT_URL}"]`)) {
      const script = document.createElement("script");
      script.src = SCRIPT_URL;
      script.async = true;
      document.head.appendChild(script);
    }
    let timer: number | undefined;
    const observer = new MutationObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => window.$WowheadPower?.refreshLinks?.(), 250);
    });
    // Only the dashboard's own content: Wowhead adds its tooltips to <body>, and refreshing links
    // because of them would hide the tooltip that just opened.
    observer.observe(document.getElementById("root") ?? document.body, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      window.clearTimeout(timer);
    };
  }, [enabled]);
}

/** Turns on Wowhead tooltips for the whole dashboard (mounted once, inside LiveProvider). */
export function WowheadTooltips() {
  const { status } = useLive();
  useWowheadTooltips(Boolean(status?.links?.wowhead_tooltips));
  return null;
}

/** An item or spell name linked to its Wowhead page (Settings: Item link / Spell link); plain text without one. */
export function WowLink({ kind, id, children, className }: { kind: WowKind; id: number | null | undefined; children: ReactNode; className?: string }) {
  const { status } = useLive();
  const url = wowheadUrl(status?.links?.[kind], id);
  if (!url) return <span className={className}>{children}</span>;
  return (
    <a href={url} target="_blank" rel="noreferrer" className={`hover:underline ${className ?? ""}`}>
      {children}
    </a>
  );
}
