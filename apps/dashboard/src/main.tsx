import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider, Link, NavProvider, useAuth, useLiveUpdates, usePath } from "./core";
import { IncidentDetailPage } from "./pages/IncidentDetail";
import { Incidents } from "./pages/Incidents";
import { Experiments, Login, Services, Settings } from "./pages/Other";
import { Overview } from "./pages/Overview";
import "./styles.css";

try {
  const t = localStorage.getItem("sentinelops.theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch { /* ignore */ }

const NAV = [
  ["/", "Overview"], ["/incidents", "Incidents"], ["/services", "Services"],
  ["/experiments", "Experiments"], ["/settings", "Settings"],
] as const;

function App() {
  const { me, logout } = useAuth();
  const [path, navigate] = usePath();
  const live = useLiveUpdates(!!me);
  if (!me) return <Login />;
  const incident = path.match(/^\/incidents\/([0-9a-f-]{36})$/);
  const page = incident ? <IncidentDetailPage id={incident[1]!} />
    : path === "/incidents" ? <Incidents />
      : path === "/services" ? <Services />
        : path === "/experiments" ? <Experiments />
          : path === "/settings" ? <Settings />
            : <Overview />;
  return (
    <NavProvider value={navigate}>
      <div className="shell">
        <nav className="nav" aria-label="Main">
          <div className="brand"><img src="/favicon.svg" width={22} height={22} alt="" /> SentinelOps</div>
          {NAV.map(([to, label]) => (
            <Link key={to} to={to} className={(to === "/" ? path === "/" : path.startsWith(to)) ? "active" : ""}>{label}</Link>
          ))}
          <div className="spacer" />
          <div className="small muted" style={{ padding: "0 10px" }}>
            <span aria-hidden="true" style={{ color: live === "live" ? "var(--good)" : "var(--warning)" }}>●</span>{" "}
            <span data-testid="stream-state">{live}</span>
          </div>
          <div className="small" style={{ padding: "4px 10px" }}>{me.name}</div>
          <button className="btn" style={{ margin: "0 10px" }} onClick={logout}>Sign out</button>
        </nav>
        <main className="main">{page}</main>
      </div>
    </NavProvider>
  );
}

const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 3000, refetchOnWindowFocus: true } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <AuthProvider><App /></AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
