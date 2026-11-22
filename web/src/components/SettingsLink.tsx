// A link to a setting. Settings are for dashboard admins, so everyone else
// sees the text with "(admins only)" instead of a link to a page they can't
// open. `plain`: just the text (e.g. a setting's name in the audit log).
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { can } from "../lib/access";
import { useMe } from "../lib/me";

export function SettingsLink({
  to,
  className,
  plain = false,
  children,
}: {
  to: string;
  className?: string;
  plain?: boolean;
  children: ReactNode;
}) {
  const me = useMe();
  if (can(me, "admin"))
    return (
      <Link to={to} className={className}>
        {children}
      </Link>
    );
  if (plain) return <span>{children}</span>;
  return (
    <span title="Settings are changed by dashboard admins">
      {children} <span className="text-faint">(admins only)</span>
    </span>
  );
}
