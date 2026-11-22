// What the signed-in user may open, mirroring webapi/access.py. Only used to
// hide what the server would refuse anyway: every request is checked there.
import type { Level, Me } from "./api";

/** A level, or "any" for everyone who can use the dashboard at all. */
export type Need = Level | "any";

export function can(me: Me, need: Need = "any"): boolean {
  return need === "any" ? me.levels.length > 0 : me.levels.includes(need);
}

export const LEVEL_LABELS: Record<Level, string> = {
  admin: "Admin",
  moderator: "Moderator",
  reviewer: "Reviewer",
  viewer: "Viewer",
};
