import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type IncidentStatus } from "../api";
import { SeverityBadge, StatusBadge } from "../components/Badges";
import { LineChart } from "../components/LineChart";
import { fmtDuration, Link, pct, since } from "../core";

const ACTIVE: IncidentStatus[] = [
  "OPEN", "TRIAGING", "INVESTIGATING", "AWAITING_APPROVAL", "REMEDIATING", "VERIFYING",
  "NEEDS_HUMAN", "FAILED",
];

export function ServiceCharts({ service, window }: { service: string; window: string }) {
  const m = useQuery({
    queryKey: ["metrics", service, window],
    queryFn: () => api.metrics(service, window),
    refetchInterval: 15_000,
  });
  const s = (name: string) => m.data?.series.find((x) => x.metric === name)?.points ?? [];
  if (m.isError) return <div className="card error">Metrics unavailable: {String(m.error)}</div>;
  return (
    <div className="grid cols-3">
      <LineChart title="Request rate" points={s("request_rate")} format={(v) => `${v.toFixed(1)}/s`} />
      <LineChart title="Error ratio (5xx)" points={s("error_ratio")} format={pct} yCap={1}
        threshold={{ value: 0.05, label: "alert 5%" }} />
      <LineChart title="Latency p95" points={s("latency_p95")}
        format={(v) => (v >= 1 ? `${v.toFixed(2)}s` : `${Math.round(v * 1000)}ms`)}
        threshold={{ value: 0.5, label: "alert 500ms" }} />
      {s("memory_rss").length > 1 && (
        <LineChart title="Resident memory" points={s("memory_rss")}
          format={(v) => `${Math.round(v / 2 ** 20)} MiB`}
          threshold={{ value: 300 * 2 ** 20, label: "alert 300 MiB" }} />
      )}
    </div>
  );
}

export function Overview() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 15_000 });
  const active = useQuery({
    queryKey: ["incidents", "active"],
    queryFn: () => api.incidents({ status: ACTIVE, limit: 20 }),
  });
  const services = useQuery({ queryKey: ["services"], queryFn: api.services });
  const [svc, setSvc] = useState("checkout");
  const [window, setWindow] = useState("30m");
  const by = stats.data?.incidents_by_status ?? {};
  const openCount = ACTIVE.reduce((n, s) => n + (by[s] ?? 0), 0);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="sub">Detection, investigation and human-approved recovery for the sandbox workload.</p>
        </div>
      </div>
      <div className="grid cols-4">
        <div className="card tile"><div className="value">{openCount}</div><div className="label">Active incidents</div></div>
        <div className="card tile"><div className="value">{by.AWAITING_APPROVAL ?? 0}</div><div className="label">Awaiting approval</div></div>
        <div className="card tile"><div className="value">{stats.data?.verified_recoveries ?? 0}</div><div className="label">Verified recoveries</div></div>
        <div className="card tile">
          <div className="value">{fmtDuration(stats.data?.mttr_seconds_p50)}</div>
          <div className="label">Median time to verified recovery</div>
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Service health</h2>
          <div className="row">
            <label className="small secondary" htmlFor="svc">Service</label>
            <select id="svc" value={svc} onChange={(e) => setSvc(e.target.value)}>
              {(services.data ?? []).map((s) => <option key={s.name}>{s.name}</option>)}
            </select>
            <label className="small secondary" htmlFor="win">Window</label>
            <select id="win" value={window} onChange={(e) => setWindow(e.target.value)}>
              {["15m", "30m", "1h", "3h"].map((w) => <option key={w}>{w}</option>)}
            </select>
          </div>
        </div>
        <ServiceCharts service={svc} window={window} />
      </div>

      <div className="card">
        <h2>Active incidents</h2>
        {active.isLoading ? <div className="empty">Loading…</div> :
          active.data?.items.length ? (
            <table>
              <thead><tr><th>Incident</th><th>Service</th><th>Severity</th><th>Status</th><th className="num">Age</th></tr></thead>
              <tbody>
                {active.data.items.map((i) => (
                  <tr key={i.id}>
                    <td><Link to={`/incidents/${i.id}`}>{i.title}</Link></td>
                    <td>{i.service}</td>
                    <td><SeverityBadge severity={i.severity} /></td>
                    <td><StatusBadge status={i.status} /></td>
                    <td className="num">{since(i.first_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No active incidents. Trigger one from Experiments.</div>}
      </div>
    </div>
  );
}
