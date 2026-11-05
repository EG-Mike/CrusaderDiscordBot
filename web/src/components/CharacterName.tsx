import type { WowClass } from "../lib/api";
import { classColor } from "../lib/wow";

/** A character's name in its class colour. */
export function CharacterName({ c, classes }: { c: { display_name: string; class_id: number | null }; classes: WowClass[] }) {
  return (
    <span className="font-medium" style={{ color: classColor(classes, c.class_id) }}>
      {c.display_name}
    </span>
  );
}
