import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type IncidentStatus } from "../api";
import { SeverityBadge, StatusBadge, statusLabel } from "../components/Badges";
import { fmtDateTime, Link } from "../core";

const ALL: IncidentStatus[] = [
  "OPEN", "TRIAGING", "INVESTIGATING", "AWAITING_APPROVAL", "REMEDIATING", "VERIFYING",
  "NEEDS_HUMAN", "REJECTED", "FAILED", "RESOLVED",
];

export function Incidents() {
  const [status, setStatus] = useState<string>("");
  const [service, setService] = useState<string>("");
  const services = useQuery({ queryKey: ["services"], queryFn: api.services });
  const q = useQuery({
    queryKey: ["incidents", status, service],
    queryFn: () => api.incidents({ status: status ? [status] : undefined, service: service || undefined, limit: 200 }),
  });
  return (
    <div className="stack">
      <div className="page-head">
        <div><h1>Incidents</h1><p className="sub">Every alert-driven incident with its full lifecycle.</p></div>
        <div className="row">
          <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {ALL.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
          <select aria-label="Service filter" value={service} onChange={(e) => setService(e.target.value)}>
            <option value="">All services</option>
            {(services.data ?? []).map((s) => <option key={s.name}>{s.name}</option>)}
          </select>
        </div>
      </div>
      <div className="card">
        {q.isLoading ? <div className="empty">Loading…</div> : q.isError ? <div className="error">{String(q.error)}</div> :
          q.data?.items.length ? (
            <table>
              <thead>
                <tr><th>Incident</th><th>Service</th><th>Severity</th><th>Status</th><th>Alert</th><th>First seen</th><th className="num">Alerts</th></tr>
              </thead>
              <tbody>
                {q.data.items.map((i) => (
                  <tr key={i.id}>
                    <td><Link to={`/incidents/${i.id}`}>{i.title}</Link>
                      <div className="muted small mono">{i.fingerprint}</div></td>
                    <td>{i.service}</td>
                    <td><SeverityBadge severity={i.severity} /></td>
                    <td><StatusBadge status={i.status} /></td>
                    <td className="small">{i.alert_active ? "firing" : "cleared"}</td>
                    <td className="small">{fmtDateTime(i.first_seen)}</td>
                    <td className="num">{i.alert_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="empty">No incidents match.</div>}
      </div>
    </div>
  );
}
