// Emoji data for the picker and previews: the bot's and the server's custom
// emoji (from the API), and the standard Unicode set with Discord-style
// shortcodes (loaded on first use, so it isn't in the main bundle).
import { useEffect, useState } from "react";
import { api, type EmojiOverview } from "./api";

export interface StandardEmoji {
  char: string;
  label: string;
  group: number;
  shortcodes: string[];
}

export interface StandardEmojiSet {
  list: StandardEmoji[];
  byShortcode: Map<string, string>;
}

let standard: Promise<StandardEmojiSet> | null = null;

export function loadStandardEmoji(): Promise<StandardEmojiSet> {
  standard ??= Promise.all([import("emojibase-data/en/compact.json"), import("emojibase-data/en/shortcodes/github.json")]).then(
    ([compact, codes]) => {
      const shortcodes = codes.default as Record<string, string | string[]>;
      const list: StandardEmoji[] = [];
      const byShortcode = new Map<string, string>();
      for (const e of compact.default as { hexcode: string; label: string; unicode: string; group?: number }[]) {
        if (e.group === undefined || e.group === 2) continue; // 2 = skin-tone/hair components
        const names = shortcodes[e.hexcode];
        const list_ = names ? (Array.isArray(names) ? names : [names]) : [];
        list.push({ char: e.unicode, label: e.label, group: e.group, shortcodes: list_ });
        list_.forEach((n) => byShortcode.set(n, e.unicode));
      }
      return { list, byShortcode };
    },
  );
  return standard;
}

/** Shortcode -> emoji, once loaded (null until then). */
export function useShortcodes() {
  const [map, setMap] = useState<Map<string, string> | null>(null);
  useEffect(() => {
    let alive = true;
    loadStandardEmoji().then((set) => alive && setMap(set.byShortcode)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return map;
}

export const STANDARD_GROUPS: [number, string][] = [
  [0, "Smileys"],
  [1, "People"],
  [3, "Nature"],
  [4, "Food"],
  [5, "Travel"],
  [6, "Activities"],
  [7, "Objects"],
  [8, "Symbols"],
  [9, "Flags"],
];

let custom: Promise<EmojiOverview> | null = null;

/** The bot's and the server's custom emoji (cached until refreshed). */
export function loadCustomEmoji(refresh = false): Promise<EmojiOverview> {
  if (refresh || !custom) custom = api.get<EmojiOverview>("/api/emoji").catch((e) => {
    custom = null;
    throw e;
  });
  return custom;
}
