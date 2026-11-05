// WoW display helpers: class colours (by class name, so they don't depend
// on a game version's class ids) and character name formatting.
import type { WowClass } from "./api";

export const CLASS_COLORS: Record<string, string> = {
  "Death Knight": "#C41E3A",
  "Demon Hunter": "#A330C9",
  Druid: "#FF7C0A",
  Evoker: "#33937F",
  Hunter: "#AAD372",
  Mage: "#3FC7EB",
  Monk: "#00FF98",
  Paladin: "#F48CBA",
  Priest: "#FFFFFF",
  Rogue: "#FFF468",
  Shaman: "#0070DD",
  Warlock: "#8788EE",
  Warrior: "#C69B6D",
};

export function className(classes: WowClass[], id: number | null) {
  return classes.find((c) => c.id === id)?.name ?? "";
}

export function classColor(classes: WowClass[], id: number | null) {
  return CLASS_COLORS[className(classes, id)] ?? undefined;
}

export function specName(classes: WowClass[], id: number | null) {
  if (id === null) return "";
  for (const c of classes) {
    const spec = c.specs.find((s) => s.id === id);
    if (spec) return spec.name;
  }
  return "";
}
