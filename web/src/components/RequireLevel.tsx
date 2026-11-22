// Shows a page only to users with the level it needs; others get a short
// explanation instead of a page whose requests the server would refuse.
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Lock } from "lucide-react";
import { can, LEVEL_LABELS, type Need } from "../lib/access";
import { useMe } from "../lib/me";

const WHO: Record<Exclude<Need, "any">, string> = {
  admin: "dashboard admins",
  moderator: "moderators",
  reviewer: "moderators and application reviewers",
  viewer: "members with viewer access",
};

export function RequireLevel({ level, children }: { level: Need; children: ReactNode }) {
  const me = useMe();
  if (can(me, level)) return <>{children}</>;
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full border border-line bg-raised text-muted">
        <Lock className="h-5 w-5" />
      </div>
      <div className="font-semibold">You don't have access to this page</div>
      <p className="max-w-md text-muted">
        It's for {level === "any" ? "dashboard users" : WHO[level]}. You're signed in as{" "}
        {LEVEL_LABELS[me.level].toLowerCase()}. Ask an admin if you think you should have access.
      </p>
      <Link to="/" className="text-gold hover:underline">
        Back to the overview
      </Link>
    </div>
  );
}
