import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { api, type Me } from "./lib/api";
import { LiveProvider } from "./lib/live";
import { Layout, NAV } from "./components/Layout";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import SettingsPage from "./pages/Settings";
import ComingSoon from "./pages/ComingSoon";
import { ToastProvider } from "./components/Toast";

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
    <ToastProvider>
    <LiveProvider>
      <Layout me={me}>
        <Routes>
          <Route path="/" element={<Overview me={me} />} />
          <Route path="/settings" element={<SettingsPage />} />
          {soon.map((item) => (
            <Route key={item.to} path={item.to} element={<ComingSoon item={item} />} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </LiveProvider>
    </ToastProvider>
  );
}
