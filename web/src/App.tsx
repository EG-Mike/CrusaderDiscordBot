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

// First visit after install: open the setup wizard once per browser session
// while required settings are missing and setup was never finished.
function SetupRedirect() {
  const { status } = useLive();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!status || status.setup_completed || !status.missing_setup.length || pathname !== "/") return;
    try {
      if (sessionStorage.getItem("setupPrompted")) return;
      sessionStorage.setItem("setupPrompted", "1");
    } catch {
      /* storage unavailable: still redirect */
    }
    navigate("/setup");
  }, [status, pathname, navigate]);
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
      <SetupRedirect />
      <WowheadTooltips />
      <Layout me={me}>
        <Routes>
          <Route path="/" element={<Overview me={me} />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/audit-log" element={<AuditLogPage />} />
          <Route path="/system-log" element={<SystemLogPage />} />
          <Route path="/announcements" element={<AnnouncementsPage />} />
          <Route path="/announcements/:id" element={<AnnouncementEditor />} />
          <Route path="/bot-messages" element={<BotMessagesPage />} />
          <Route path="/emoji" element={<EmojiPage />} />
          <Route path="/maintenance" element={<MaintenancePage />} />
          <Route path="/polls" element={<PollsPage />} />
          <Route path="/roster" element={<RosterPage />} />
          <Route path="/leveling" element={<LevelingPage />} />
          <Route path="/lookup" element={<LookupPage />} />
          <Route path="/crafting" element={<CraftingPage />} />
          <Route path="/auction-prices" element={<AuctionPricesPage />} />
          <Route path="/rank-sync" element={<RankSyncPage />} />
          <Route path="/applications" element={<ApplicationsPage />} />
          <Route path="/applications/statistics" element={<CardStatisticsPage />} />
          <Route path="/applications/:id" element={<ApplicationPage />} />
          {soon.map((item) => (
            <Route key={item.to} path={item.to} element={<ComingSoon item={item} />} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </LiveProvider>
    </ToastProvider>
    </MeContext.Provider>
  );
}
