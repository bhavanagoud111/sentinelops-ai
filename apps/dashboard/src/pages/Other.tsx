import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type IncidentStatus, type ServiceInfo } from "../api";
import { Badge } from "../components/Badges";
import { Link, useAuth } from "../core";
import { ServiceCharts } from "./Overview";

const ACTIVE: IncidentStatus[] = ["OPEN", "TRIAGING", "INVESTIGATING", "AWAITING_APPROVAL",
  "REMEDIATING", "VERIFYING", "NEEDS_HUMAN", "FAILED"];

// ------------------------------------------------------------------ services / topology
const POS: Record<string, [number, number]> = {
  loadgen: [70, 110], checkout: [260, 110], inventory: [470, 50], payment: [470, 170], worker: [260, 230],
};

function Topology({ services, unhealthy }: { services: ServiceInfo[]; unhealthy: Set<string> }) {
  const edges: [string, string][] = [["loadgen", "checkout"]];
  services.forEach((s) => s.dependencies.forEach((d) => edges.push([s.name, d])));
  const node = (name: string, label: string) => {
    const p = POS[name];
    if (!p) return null;
    const bad = unhealthy.has(name);
    return (
      <g key={name} transform={`translate(${p[0]},${p[1]})`}>
        <rect x={-58} y={-20} width={116} height={40} rx={8} fill="var(--surface-1)"
          stroke={bad ? "var(--critical)" : "var(--border)"} strokeWidth={bad ? 2 : 1} />
        <text textAnchor="middle" y={-2} fontSize={12.5} fontWeight={700} fill="var(--text-primary)">{label}</text>
        <text textAnchor="middle" y={13} fontSize={10.5} fill={bad ? "var(--critical-ink)" : "var(--text-muted)"}>
          {bad ? "✕ active incident" : name === "loadgen" ? "synthetic traffic" : "✓ healthy"}
        </text>
      </g>
    );
  };
  return (
    <svg viewBox="0 0 560 270" role="img" aria-label="Service dependency graph" style={{ width: "100%", maxWidth: 640 }}>
      <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="var(--text-muted)" /></marker></defs>
      {edges.map(([a, b]) => {
        const pa = POS[a], pb = POS[b];
        if (!pa || !pb) return null;
        return <line key={`${a}-${b}`} x1={pa[0] + 58} y1={pa[1]} x2={pb[0] - 60} y2={pb[1]}
          stroke="var(--text-muted)" strokeWidth={1.5} markerEnd="url(#arr)" />;
      })}
      {node("loadgen", "loadgen")}
      {services.map((s) => node(s.name, s.name))}
    </svg>
  );
}

export function Services() {
  const services = useQuery({ queryKey: ["services"], queryFn: api.services });
  const active = useQuery({ queryKey: ["incidents", "active"], queryFn: () => api.incidents({ status: ACTIVE, limit: 100 }) });
  const unhealthy = new Set((active.data?.items ?? []).map((i) => i.service));
  const [sel, setSel] = useState<string | null>(null);
  return (
    <div className="stack">
      <div className="page-head"><div><h1>Services</h1><p className="sub">Sandbox workload in the <span className="mono">sentinelops-demo</span> namespace.</p></div></div>
      <div className="card"><Topology services={services.data ?? []} unhealthy={unhealthy} /></div>
      <div className="card">
        <table>
          <thead><tr><th>Service</th><th>Owner</th><th>Criticality</th><th>Depends on</th><th>Status</th></tr></thead>
          <tbody>{(services.data ?? []).map((s) => (
            <tr key={s.name}>
              <td><button className="btn" onClick={() => setSel(sel === s.name ? null : s.name)}>{s.name}</button>
                <div className="small muted">{s.description}</div></td>
              <td>{s.owner}</td><td>{s.criticality}</td><td>{s.dependencies.join(", ") || "—"}</td>
              <td>{unhealthy.has(s.name) ? <Badge tone="critical" label="incident" /> : <Badge tone="good" label="healthy" />}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {sel && <ServiceCharts service={sel} window="1h" />}
    </div>
  );
}

// ------------------------------------------------------------------ experiments
export function Experiments() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: api.scenarios });
  const [msg, setMsg] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: (sid: string) => (sid === "reset" ? api.resetScenarios() : api.startScenario(sid)),
    onSuccess: (_, sid) => { setMsg(sid === "reset" ? "Baseline restored." : `Scenario ${sid} started — watch Incidents (detection takes 1–3 minutes).`); void qc.invalidateQueries({ queryKey: ["incidents"] }); },
    onError: (e) => setMsg(`Failed: ${(e as Error).message}`),
  });
  const allowed = can("run_experiments");
  return (
    <div className="stack">
      <div className="page-head">
        <div><h1>Experiments</h1><p className="sub">Inject a fixed sandbox fault and watch detection → diagnosis → approval → recovery.</p></div>
        <button className="btn" disabled={!allowed || start.isPending} onClick={() => start.mutate("reset")}>Reset baseline</button>
      </div>
      {!allowed && <div className="callout warn small">Running experiments requires the admin role (switch user to <span className="mono">ada.admin</span> in Settings).</div>}
      {msg && <div className="callout small">{msg} <Link to="/incidents">Open incidents →</Link></div>}
      {scenarios.isError && <div className="error">{String(scenarios.error)}</div>}
      <div className="grid cols-2">
        {(scenarios.data ?? []).map((s) => (
          <div key={s.id} className="card stack" style={{ gap: 8 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h2 style={{ margin: 0 }}>{s.id} · {s.title}</h2>
              <button className="btn primary" disabled={!allowed || start.isPending} onClick={() => start.mutate(s.id)}>Inject</button>
            </div>
            <p className="small" style={{ margin: 0 }}>{s.description}</p>
            <dl className="kv small"><dt>Alert</dt><dd className="mono">{s.expected_alert}</dd><dt>Expected</dt><dd>{s.expected_outcome}</dd></dl>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ settings / login
export function UserSwitcher() {
  const { me, login } = useAuth();
  const users = useQuery({ queryKey: ["dev-users"], queryFn: api.devUsers, retry: false });
  if (users.isError) return <p className="muted small">Dev login is disabled on this deployment.</p>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {(users.data ?? []).map((u) => (
        <button key={u.username} className={`btn ${me?.subject === u.username ? "primary" : ""}`}
          style={{ textAlign: "left" }} onClick={() => void login(u.username)}>
          {u.name} <span className="small" style={{ opacity: .8 }}>— {u.roles.join(", ")}</span>
        </button>
      ))}
    </div>
  );
}

export function Settings() {
  const { me } = useAuth();
  const sys = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 15_000 });
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? "system");
  const applyTheme = (t: string) => {
    setTheme(t);
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    try { localStorage.setItem("sentinelops.theme", t); } catch { /* ignore */ }
  };
  return (
    <div className="stack">
      <div className="page-head"><div><h1>Settings</h1><p className="sub">Identity, integrations and safety mode.</p></div></div>
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Signed in as</h2>
          <dl className="kv small"><dt>User</dt><dd>{me?.name} (<span className="mono">{me?.subject}</span>)</dd>
            <dt>Roles</dt><dd>{me?.roles.join(", ")}</dd><dt>Permissions</dt><dd>{me?.permissions.join(", ")}</dd></dl>
          <h3>Switch demo user</h3>
          <UserSwitcher />
        </div>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Integrations</h2>
          <table><tbody>
            {Object.entries(sys.data?.integrations ?? {}).map(([k, ok]) => (
              <tr key={k}><td>{k}</td><td>{ok ? <Badge tone="good" label="reachable" /> : <Badge tone="critical" label="down" />}</td></tr>
            ))}
            <tr><td>Remediation mode</td><td><Badge tone={sys.data?.remediation_mode === "sandbox" ? "warning" : "good"} label={sys.data?.remediation_mode ?? "…"} /></td></tr>
            <tr><td>Investigation model</td><td className="mono">{sys.data?.llm_provider}</td></tr>
            <tr><td>Live stream clients</td><td className="mono">{sys.data?.stream_subscribers}</td></tr>
          </tbody></table>
          {sys.data?.grafana_url && <a href={sys.data.grafana_url} target="_blank" rel="noreferrer">Open Grafana ↗</a>}
          <h3>Theme</h3>
          <div className="row">{["system", "light", "dark"].map((t) => (
            <button key={t} className={`btn ${theme === t ? "primary" : ""}`} onClick={() => applyTheme(t)}>{t}</button>
          ))}</div>
        </div>
      </div>
    </div>
  );
}

export function Login() {
  return (
    <div style={{ display: "grid", placeItems: "center", minHeight: "100vh", padding: 16 }}>
      <div className="card stack" style={{ maxWidth: 440, width: "100%" }}>
        <h1>SentinelOps</h1>
        <p className="sub">AI-assisted incident response. Pick a demo identity — roles decide what you can do
          (operators request, approvers decide, only admins run experiments).</p>
        <UserSwitcher />
      </div>
    </div>
  );
}
