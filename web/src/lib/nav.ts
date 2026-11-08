// The dashboard's pages, grouped as in the sidebar. Also used by the
// command palette and the breadcrumb.
import {
  BarChart3,
  CalendarCheck,
  ClipboardList,
  FileText,
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

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  soon?: boolean;
  /** Small count shown next to the label (e.g. errors waiting to be read). */
  count?: (status: Status, me: Me) => number;
  /** Left out of the sidebar (still in the command palette), e.g. Setup once it's done. */
  hidden?: (status: Status) => boolean;
}

export const NAV: { group: string; items: NavItem[] }[] = [
  { group: "", items: [{ to: "/", label: "Overview", icon: LayoutDashboard }] },
  {
    group: "Communication",
    items: [
      { to: "/announcements", label: "Announcements", icon: Megaphone },
      { to: "/bot-messages", label: "Bot messages", icon: MessageSquareText },
      { to: "/emoji", label: "Emoji", icon: Smile },
      { to: "/polls", label: "Polls", icon: Vote },
    ],
  },
  {
    group: "Members",
    items: [
      { to: "/roster", label: "Guild roster", icon: Users },
      { to: "/leveling", label: "Leveling", icon: TrendingUp },
      { to: "/lookup", label: "Character lookup", icon: UserSearch },
      { to: "/applications", label: "Applications", icon: ClipboardList, count: (status) => status.counts.pending_applications },
      { to: "/attendance", label: "Attendance", icon: CalendarCheck, soon: true },
    ],
  },
  {
    group: "Raids",
    items: [
      { to: "/raid-summaries", label: "Raid summaries", icon: Swords, soon: true },
      { to: "/raid-logs", label: "Raid logs", icon: FileText, soon: true },
      { to: "/records", label: "Records", icon: Trophy, soon: true },
      { to: "/tier-recap", label: "Tier recap", icon: BarChart3, soon: true },
    ],
  },
  {
    group: "System",
    items: [
      // The setup guide is for the first run; afterwards it's in Settings, System.
      { to: "/setup", label: "Setup", icon: Rocket, hidden: (status) => status.setup_completed },
      { to: "/audit-log", label: "Audit log", icon: ScrollText },
      {
        to: "/system-log",
        label: "System log",
        icon: Server,
        count: (status, me) => (me.levels.includes("admin") ? status.system.errors_24h : 0) + status.system.crashes_7d,
      },
      { to: "/settings", label: "Settings", icon: Settings },
      { to: "/maintenance", label: "Maintenance", icon: Wrench },
    ],
  },
];

