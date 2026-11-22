import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { api, type Me } from "./lib/api";
import { LiveProvider, useLive } from "./lib/live";
import { WowheadTooltips } from "./lib/wowhead";
import { MeContext } from "./lib/me";
import SetupPage from "./pages/Setup";
import { Layout } from "./components/Layout";
import { NAV } from "./lib/nav";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import SettingsPage from "./pages/Settings";
import ComingSoon from "./pages/ComingSoon";
import AuditLogPage from "./pages/AuditLog";
import SystemLogPage from "./pages/SystemLog";
import AnnouncementsPage from "./pages/Announcements";
import AnnouncementEditor from "./pages/AnnouncementEditor";
import BotMessagesPage from "./pages/BotMessages";
import EmojiPage from "./pages/Emoji";
import MaintenancePage from "./pages/Maintenance";
import PollsPage from "./pages/Polls";
import RosterPage from "./pages/Roster";
import LevelingPage from "./pages/Leveling";
import LookupPage from "./pages/Lookup";
import CraftingPage from "./pages/Crafting";
import AuctionPricesPage from "./pages/AuctionPrices";
import RankSyncPage from "./pages/RankSync";
import CardStatisticsPage from "./pages/CardStatistics";
import { ApplicationPage, ApplicationsPage } from "./pages/Applications";
import { ToastProvider } from "./components/Toast";
import { RequireLevel } from "./components/RequireLevel";
import { can } from "./lib/access";

// First visit after install: open the setup wizard once per browser session
// while required settings are missing and setup was never finished.
function SetupRedirect({ me }: { me: Me }) {
  const { status } = useLive();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!can(me, "admin") || !status || status.setup_completed || !status.missing_setup.length || pathname !== "/") return;
    try {
      if (sessionStorage.getItem("setupPrompted")) return;
      sessionStorage.setItem("setupPrompted", "1");
    } catch {
      /* storage unavailable: still redirect */
    }
    navigate("/setup");
  }, [me, status, pathname, navigate]);
  return null;
}

export default function App() {
  const { pathname } = useLocation();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const onLogin = pathname === "/login";

  useEffect(() => {
    if (onLogin) return;
    api
      .get<Me>("/api/me")
      .then(setMe)
      .catch(() => setMe(null));
  }, [onLogin]);

  if (pathname === "/login") return <Login />;
  if (me === undefined)
    return (
      <div className="flex h-full items-center justify-center text-faint">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  if (me === null) return <Navigate to="/login" replace />;

  const soon = NAV.flatMap((g) => g.items).filter((i) => i.soon);
  return (
    <MeContext.Provider value={me}>
    <ToastProvider>
    <LiveProvider>
      <SetupRedirect me={me} />
      <WowheadTooltips />
      <Layout me={me}>
        <Routes>
          <Route path="/" element={<Overview me={me} />} />
          <Route path="/setup" element={<RequireLevel level="admin"><SetupPage /></RequireLevel>} />
          <Route path="/settings" element={<RequireLevel level="admin"><SettingsPage /></RequireLevel>} />
          <Route path="/audit-log" element={<RequireLevel level="moderator"><AuditLogPage /></RequireLevel>} />
          <Route path="/system-log" element={<RequireLevel level="moderator"><SystemLogPage /></RequireLevel>} />
          <Route path="/announcements" element={<RequireLevel level="moderator"><AnnouncementsPage /></RequireLevel>} />
          <Route path="/announcements/:id" element={<RequireLevel level="moderator"><AnnouncementEditor /></RequireLevel>} />
          <Route path="/bot-messages" element={<RequireLevel level="moderator"><BotMessagesPage /></RequireLevel>} />
          <Route path="/emoji" element={<RequireLevel level="moderator"><EmojiPage /></RequireLevel>} />
          <Route path="/maintenance" element={<RequireLevel level="moderator"><MaintenancePage /></RequireLevel>} />
          <Route path="/polls" element={<RequireLevel level="moderator"><PollsPage /></RequireLevel>} />
          <Route path="/roster" element={<RequireLevel level="viewer"><RosterPage /></RequireLevel>} />
          <Route path="/leveling" element={<RequireLevel level="viewer"><LevelingPage /></RequireLevel>} />
          <Route path="/lookup" element={<RequireLevel level="reviewer"><LookupPage /></RequireLevel>} />
          <Route path="/crafting" element={<RequireLevel level="viewer"><CraftingPage /></RequireLevel>} />
          <Route path="/auction-prices" element={<RequireLevel level="moderator"><AuctionPricesPage /></RequireLevel>} />
          <Route path="/rank-sync" element={<RequireLevel level="moderator"><RankSyncPage /></RequireLevel>} />
          <Route path="/applications" element={<RequireLevel level="reviewer"><ApplicationsPage /></RequireLevel>} />
          <Route path="/applications/statistics" element={<RequireLevel level="moderator"><CardStatisticsPage /></RequireLevel>} />
          <Route path="/applications/:id" element={<RequireLevel level="reviewer"><ApplicationPage /></RequireLevel>} />
          {soon.map((item) => (
            <Route key={item.to} path={item.to} element={<RequireLevel level={item.level}><ComingSoon item={item} /></RequireLevel>} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </LiveProvider>
    </ToastProvider>
    </MeContext.Provider>
  );
}
