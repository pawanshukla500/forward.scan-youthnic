import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { api, setUnauthorizedHandler, type User } from "./api";
import { ChangePasswordPage } from "./components/ChangePassword";
import { Shell } from "./components/Shell";
import { Spinner } from "./components/ui";
import { startLive, stopLive } from "./live";
import Admin from "./pages/Admin";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Marketplaces from "./pages/Marketplaces";
import Pending from "./pages/Pending";
import ScanStation from "./pages/ScanStation";
import ChannelPicker from "./pages/ChannelPicker";
import Scans from "./pages/Scans";

interface AuthCtx {
  user: User | null;
  setUser: (u: User | null) => void;
  logout: () => Promise<void>;
}

const Auth = createContext<AuthCtx>({ user: null, setUser: () => {}, logout: async () => {} });
export const useAuth = () => useContext(Auth);
export const isSupervisor = (u: User | null) => !!u && (u.role === "admin" || u.role === "supervisor");

function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    api<{ user: User }>("/api/auth/me")
      .then((r) => setUser(r.user))
      .catch(() => setUser(null))
      .finally(() => setReady(true));
  }, []);

  useEffect(() => {
    // live updates start once the person may use the app (not while they must still choose a password)
    if (user && !user.must_change_password) startLive();
    else stopLive();
  }, [user]);

  const logout = useCallback(async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    setUser(null);
  }, []);

  if (!ready)
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner />
      </div>
    );
  return <Auth.Provider value={{ user, setUser, logout }}>{children}</Auth.Provider>;
}

function Protected({ children, sup }: { children: ReactNode; sup?: boolean }) {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password) return <ChangePasswordPage />; // an admin set the password: choose your own first
  if (sup && !isSupervisor(user)) return <Navigate to="/scan" replace />;
  return <Shell>{children}</Shell>;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/scan" element={<Protected><ChannelPicker /></Protected>} />
          <Route path="/scan/:channelId" element={<Protected><ScanStation /></Protected>} />
          <Route path="/dashboard" element={<Protected><Dashboard /></Protected>} />
          <Route path="/marketplaces" element={<Protected><Marketplaces /></Protected>} />
          <Route path="/scans" element={<Protected><Scans /></Protected>} />
          <Route path="/pending" element={<Protected><Pending /></Protected>} />
          <Route path="/admin" element={<Protected sup><Admin /></Protected>} />
          <Route path="*" element={<Navigate to="/scan" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
