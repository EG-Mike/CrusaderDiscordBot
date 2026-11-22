// The dashboard's pages, grouped as in the sidebar. Also used by the
// command palette and the breadcrumb.
import {
  BarChart3,
  CalendarCheck,
  Coins,
  ClipboardList,
  FileText,
  Hammer,
  ChevronsUp,
  LayoutDashboard,
  Megaphone,
  MessageSquareText,
  Rocket,
  ScrollText,
  Server,
  Settings,
  Smile,
  Swords,
  TrendingUp,
  Trophy,
  UserSearch,
  Users,
  Vote,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { Me, Status } from "./api";
import { can, type Need } from "./access";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Who may open it (the server checks the page's requests the same way). */
  level: Need;
  soon?: boolean;
  /** Small count shown next to the label (e.g. errors waiting to be read). */
  count?: (status: Status, me: Me) => number;
  /** Left out of the sidebar (still in the command palette), e.g. Setup once it's done. */
  hidden?: (status: Status) => boolean;
}

export const NAV: { group: string; items: NavItem[] }[] = [
  { group: "", items: [{ to: "/", label: "Overview", icon: LayoutDashboard, level: "any" }] },
  {
    group: "Communication",
    items: [
      { to: "/announcements", label: "Announcements", icon: Megaphone, level: "moderator" },
      { to: "/bot-messages", label: "Bot messages", icon: MessageSquareText, level: "moderator" },
      { to: "/emoji", label: "Emoji", icon: Smile, level: "moderator" },
      { to: "/polls", label: "Polls", icon: Vote, level: "moderator" },
    ],
  },
  {
    group: "Members",
    items: [
      { to: "/roster", label: "Guild roster", icon: Users, level: "viewer" },
      { to: "/leveling", label: "Leveling", icon: TrendingUp, level: "viewer" },
      { to: "/lookup", label: "Character lookup", icon: UserSearch, level: "reviewer" },
      { to: "/rank-sync", label: "Rank sync", icon: ChevronsUp, level: "moderator" },
      { to: "/crafting", label: "Crafting", icon: Hammer, level: "viewer" },
      { to: "/auction-prices", label: "Auction prices", icon: Coins, level: "moderator" },
      { to: "/applications", label: "Applications", icon: ClipboardList, level: "reviewer", count: (status) => status.counts.pending_applications },
      { to: "/attendance", label: "Attendance", icon: CalendarCheck, level: "moderator", soon: true },
    ],
  },
  {
    group: "Raids",
    items: [
      { to: "/raid-summaries", label: "Raid summaries", icon: Swords, level: "moderator", soon: true },
      { to: "/raid-logs", label: "Raid logs", icon: FileText, level: "moderator", soon: true },
      { to: "/records", label: "Records", icon: Trophy, level: "moderator", soon: true },
      { to: "/tier-recap", label: "Tier recap", icon: BarChart3, level: "moderator", soon: true },
    ],
  },
  {
    group: "System",
    items: [
      // The setup guide is for the first run; afterwards it's in Settings, System.
      { to: "/setup", label: "Setup", icon: Rocket, level: "admin", hidden: (status) => status.setup_completed },
      { to: "/audit-log", label: "Audit log", icon: ScrollText, level: "moderator" },
      {
        to: "/system-log",
        label: "System log",
        icon: Server,
        level: "moderator",
        count: (status, me) => (me.levels.includes("admin") ? status.system.errors_24h : 0) + status.system.crashes_7d,
      },
      { to: "/settings", label: "Settings", icon: Settings, level: "admin" },
      { to: "/maintenance", label: "Maintenance", icon: Wrench, level: "moderator" },
    ],
  },
];


/** The sidebar for this user: only the pages they may open, and no empty groups. */
export function navFor(me: Me): { group: string; items: NavItem[] }[] {
  return NAV.map(({ group, items }) => ({ group, items: items.filter((item) => can(me, item.level)) })).filter(
    (g) => g.items.length > 0,
  );
}
