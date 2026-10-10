// Auth context, a minimal pushState router, live updates (SSE) and formatting helpers.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from "react";
import { api, getToken, setToken, type Me } from "./api";

// ------------------------------------------------------------------ auth
interface AuthState {
  me: Me | null;
  login: (username: string) => Promise<void>;
  logout: () => void;
  can: (permission: string) => boolean;
}
const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [token, setTok] = useState<string | null>(getToken());
  const me = useQuery({ queryKey: ["me", token], queryFn: api.me, enabled: !!token, retry: false });
  useEffect(() => {
    if (me.isError) { setToken(null); setTok(null); }
  }, [me.isError]);
  const login = useCallback(async (username: string) => {
    const res = await api.devLogin(username);
    setToken(res.access_token);
    setTok(res.access_token);
    qc.clear();
  }, [qc]);
  const logout = useCallback(() => { setToken(null); setTok(null); qc.clear(); }, [qc]);
  const value = useMemo<AuthState>(() => ({
    me: me.data ?? null,
    login,
    logout,
    can: (p) => !!me.data?.permissions.includes(p),
  }), [me.data, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

// ------------------------------------------------------------------ router
export function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const on = () => setPath(window.location.pathname);
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  const navigate = useCallback((to: string) => {
    window.history.pushState({}, "", to);
    setPath(to);
    window.scrollTo(0, 0);
  }, []);
  return [path, navigate];
}

const NavContext = createContext<(to: string) => void>(() => {});
export const NavProvider = NavContext.Provider;

export function Link({ to, children, className }: { to: string; children: ReactNode; className?: string }) {
  const navigate = useContext(NavContext);
  return (
    <a href={to} className={className} onClick={(e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    }}>{children}</a>
  );
}

// ------------------------------------------------------------------ live updates
/** Subscribe to the SSE stream and invalidate affected queries. Returns connection state. */
export function useLiveUpdates(enabled: boolean): "live" | "connecting" | "offline" {
  const qc = useQueryClient();
  const [state, setState] = useState<"live" | "connecting" | "offline">("connecting");
  useEffect(() => {
    const token = getToken();
    if (!enabled || !token) return;
    const es = new EventSource(`/api/v1/events/stream?access_token=${encodeURIComponent(token)}`);
    const onMsg = (ev: MessageEvent<string>) => {
      try {
        const msg = JSON.parse(ev.data) as { incident_id?: string };
        void qc.invalidateQueries({ queryKey: ["incidents"] });
        void qc.invalidateQueries({ queryKey: ["stats"] });
        if (msg.incident_id) void qc.invalidateQueries({ queryKey: ["incident", msg.incident_id] });
      } catch { /* ignore malformed */ }
    };
    es.addEventListener("ready", () => setState("live"));
    for (const t of ["sentinel.incidents.v1", "sentinel.investigations.v1",
      "sentinel.approvals.v1", "sentinel.actions.v1"]) es.addEventListener(t, onMsg);
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? "offline" : "connecting");
    return () => es.close();
  }, [enabled, qc]);
  return state;
}

// ------------------------------------------------------------------ formatting
export const fmtTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
export const fmtDateTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
export const since = (iso: string) => fmtDuration((Date.now() - new Date(iso).getTime()) / 1000);
export const pct = (v: number) => `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
