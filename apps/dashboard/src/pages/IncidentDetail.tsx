import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  api, type Approval, type Evidence, type Execution, type IncidentStatus, type Investigation,
} from "../api";
import { Badge, Confidence, SeverityBadge, StatusBadge, VerdictBadge } from "../components/Badges";
import { fmtDateTime, fmtDuration, fmtTime, since, useAuth } from "../core";
import { ServiceCharts } from "./Overview";

const HAPPY: IncidentStatus[] = [
  "OPEN", "TRIAGING", "INVESTIGATING", "AWAITING_APPROVAL", "REMEDIATING", "VERIFYING", "RESOLVED",
];
const ALT: Partial<Record<IncidentStatus, string>> = {
  NEEDS_HUMAN: "Needs human", REJECTED: "Rejected", FAILED: "Failed",
};

function Lifecycle({ status, history }: { status: IncidentStatus; history: string[] }) {
  const alt = ALT[status];
  const reached = new Set(history);
  return (
    <div className="stepper" aria-label="Incident lifecycle">
      {HAPPY.map((s) => {
        const cls = s === status ? "current" : reached.has(s) ? "done" : "";
        return <span key={s} className={`step ${cls}`}>{reached.has(s) && s !== status ? "✓ " : ""}{s.replace("_", " ").toLowerCase()}</span>;
      })}
      {alt && <span className="step alt">⚠ {alt}</span>}
    </div>
  );
}

function grafanaLink(base: string, e: Evidence): string | null {
  const range = { from: String(new Date(e.range_start).getTime()), to: String(new Date(e.range_end).getTime()) };
  let pane: Record<string, unknown> | null = null;
  if (e.source === "prometheus") {
    pane = { datasource: "prometheus", queries: [{ refId: "A", datasource: { type: "prometheus", uid: "prometheus" }, expr: e.query.split("\n")[0] }], range };
  } else if (e.source === "loki") {
    pane = { datasource: "loki", queries: [{ refId: "A", datasource: { type: "loki", uid: "loki" }, expr: e.query }], range };
  } else if (e.source === "tempo") {
    pane = { datasource: "tempo", queries: [{ refId: "A", datasource: { type: "tempo", uid: "tempo" }, queryType: "traceql", query: e.query }], range };
  }
  if (!pane) return null;
  return `${base}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify({ a: pane }))}`;
}

function EvidenceTable({ evidence, grafana, highlight }: { evidence: Evidence[]; grafana: string; highlight: Set<string> }) {
  const [onlyAnomalous, setOnly] = useState(true);
  const rows = evidence.filter((e) => !onlyAnomalous || e.anomalous || highlight.has(e.evidence_id));
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>Evidence <span className="muted small">({evidence.length} immutable items)</span></h2>
        <label className="row small"><input type="checkbox" checked={onlyAnomalous} onChange={(e) => setOnly(e.target.checked)} /> anomalous / cited only</label>
      </div>
      <table style={{ marginTop: 8 }}>
        <thead><tr><th>Evidence</th><th>Source</th><th>Summary</th><th></th></tr></thead>
        <tbody>
          {rows.map((e) => {
            const link = grafanaLink(grafana, e);
            return (
              <tr key={e.evidence_id} id={e.evidence_id} style={highlight.has(e.evidence_id) ? { background: "var(--surface-2)" } : undefined}>
                <td className="small"><div className="mono">{e.evidence_id}</div>
                  <div className="row">{e.anomalous ? <Badge tone="serious" label="anomalous" /> : <Badge tone="neutral" label="normal" />}</div></td>
                <td className="small">{e.kind}<div className="muted">{e.service} · {e.source}</div></td>
                <td className="small">{e.summary}{e.error && <div className="error">unavailable: {e.error}</div>}
                  <details><summary className="small">query &amp; signals</summary>
                    <pre className="block">{e.query}{"\n\n"}{JSON.stringify(e.signals, null, 1)}</pre></details></td>
                <td className="small">{link && <a href={link} target="_blank" rel="noreferrer">Grafana ↗</a>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {!rows.length && <div className="empty">No evidence yet.</div>}
    </div>
  );
}

function Diagnosis({ inv, onCite }: { inv: Investigation; onCite: (ids: string[]) => void }) {
  return (
    <div className="card stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>AI diagnosis</h2>
        <span className="row small muted">
          <Badge tone={inv.status === "proposed" ? "good" : inv.status === "failed" ? "critical" : "warning"} label={inv.status} />
          provider: <span className="mono">{inv.provider}</span>
        </span>
      </div>
      <p style={{ margin: 0, whiteSpace: "pre-line" }}>{inv.summary || "Investigation running…"}</p>
      {inv.error && <div className="callout bad small">{inv.error}</div>}
      {inv.hypotheses.map((h) => (
        <div key={h.key} className="callout" style={{ borderColor: h.verdict === "rejected" ? "var(--critical)" : h.verdict === "downgraded" ? "var(--warning)" : "var(--good)" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{h.key}: {h.cause_category.replaceAll("_", " ")} @ {h.suspected_service}</strong>
            <span className="row"><VerdictBadge verdict={h.verdict} /><Confidence value={h.adjusted_confidence} /></span>
          </div>
          <div className="small" style={{ margin: "4px 0" }}>{h.description}</div>
          <div className="small muted">Critic: {h.critic_reasons.join("; ")}</div>
          {h.uncertainty && <div className="small muted">Uncertainty: {h.uncertainty}</div>}
          <div className="row small" style={{ marginTop: 4 }}>
            Evidence: {h.evidence_ids.map((id) => (
              <a key={id} href={`#${id}`} className="mono" onClick={() => onCite(h.evidence_ids)}>{id}</a>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function AgentActivity({ inv }: { inv: Investigation }) {
  return (
    <div className="card">
      <h2>Agent activity</h2>
      <ol className="timeline">
        {inv.steps.map((s, i) => {
          const ms = new Date(s.finished_at).getTime() - new Date(s.started_at).getTime();
          return (
            <li key={i}>
              <time>{fmtTime(s.started_at)}</time>
              <div>
                <strong>{s.node.replaceAll("_", " ")}</strong> <span className="muted small">{ms} ms{s.model ? ` · ${s.model}` : ""}
                  {Object.keys(s.usage).length ? ` · ${Object.entries(s.usage).map(([k, v]) => `${k}=${v}`).join(" ")}` : ""}</span>
                <div className="small secondary">{s.summary}</div>
                {s.tool_calls.length > 0 && (
                  <details><summary className="small">{s.tool_calls.length} tool call(s)</summary>
                    <pre className="block">{JSON.stringify(s.tool_calls, null, 1)}</pre></details>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Remediation({ incidentId, status, inv, approvals, executions }: {
  incidentId: string; status: IncidentStatus; inv: Investigation | undefined;
  approvals: Approval[]; executions: Execution[];
}) {
  const { me, can } = useAuth();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["incident", incidentId] });
  const run = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => { setErr(null); void refresh(); },
    onError: (e) => setErr(String((e as Error).message)),
  });
  const proposal = inv?.proposal;
  const live = approvals.find((a) => a.status === "pending" || a.status === "approved");
  const latestExec = executions[0];

  return (
    <div className="card stack">
      <h2 style={{ margin: 0 }}>Remediation</h2>
      {!proposal ? (
        <div className="callout warn small">No allowlisted remediation proposed. SentinelOps never guesses an action — a human decides the next step.</div>
      ) : (
        <>
          <dl className="kv small">
            <dt>Runbook</dt><dd><strong>{proposal.runbook_id}</strong></dd>
            <dt>Parameters</dt><dd className="mono">{JSON.stringify(proposal.params)}</dd>
            <dt>Risk</dt><dd><Badge tone={proposal.risk === "low" ? "good" : proposal.risk === "medium" ? "warning" : "critical"} label={proposal.risk} /></dd>
            <dt>Blast radius</dt><dd className="mono">{JSON.stringify(proposal.blast_radius)}</dd>
            <dt>Bound revision</dt><dd>{proposal.target_revision ?? "unknown"}</dd>
            <dt>Rationale</dt><dd>{proposal.rationale}</dd>
          </dl>
          {status === "AWAITING_APPROVAL" && !live && (
            <button className="btn primary" disabled={!can("request_approval") || run.isPending}
              title={can("request_approval") ? "" : "requires the operator role"}
              onClick={() => run.mutate(() => api.requestApproval(incidentId))}>Request approval</button>
          )}
        </>
      )}
      {live && (
        <div className="callout small">
          <div><strong>Approval {live.status}</strong> · requested by <span className="mono">{live.requested_by}</span> · expires {fmtTime(live.expires_at)}</div>
          {live.approved_by && <div>Approved by <span className="mono">{live.approved_by}</span>: {live.decision_reason}</div>}
          <div className="muted">Bound to request hash <span className="mono">{live.request_hash.slice(0, 16)}…</span></div>
          {live.status === "pending" && (
            <div className="stack" style={{ marginTop: 8, gap: 8 }}>
              {me?.subject === live.requested_by && <div className="muted">Four-eyes rule: someone else must decide.</div>}
              <textarea placeholder="Reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
              <div className="row">
                <button className="btn primary" disabled={!can("decide_approval") || me?.subject === live.requested_by || reason.length < 3 || run.isPending}
                  onClick={() => run.mutate(() => api.decide(live.id, "approve", reason))}>Approve</button>
                <button className="btn danger" disabled={!can("decide_approval") || me?.subject === live.requested_by || reason.length < 3 || run.isPending}
                  onClick={() => run.mutate(() => api.decide(live.id, "reject", reason))}>Reject</button>
              </div>
            </div>
          )}
          {live.status === "approved" && status === "AWAITING_APPROVAL" && (
            <button className="btn primary" style={{ marginTop: 8 }} disabled={!can("execute") || run.isPending}
              onClick={() => run.mutate(() => api.execute(incidentId, live.runbook_id, live.id))}>Execute {live.runbook_id}</button>
          )}
        </div>
      )}
      {latestExec && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row"><strong>Execution</strong>
            <Badge tone={latestExec.status === "succeeded" ? "good" : latestExec.status === "blocked" || latestExec.status === "failed" ? "critical" : "info"} label={latestExec.status} />
            {latestExec.verification.recovered != null && <Badge tone={latestExec.verification.recovered ? "good" : "critical"} label={latestExec.verification.recovered ? "recovery verified" : "not recovered"} />}
          </div>
          {latestExec.command_template && <code>{latestExec.command_template}</code>}
          {latestExec.error && <div className="error small">{latestExec.error}</div>}
          {latestExec.policy.checks && (
            <details open={latestExec.status === "blocked"}><summary className="small">Policy checks ({latestExec.policy.checks.filter((c) => c.passed).length}/{latestExec.policy.checks.length} passed)</summary>
              <table><tbody>{latestExec.policy.checks.map((c) => (
                <tr key={c.name}><td className="small">{c.passed ? <Badge tone="good" label="pass" /> : <Badge tone="critical" label="fail" />}</td>
                  <td className="small mono">{c.name}</td><td className="small secondary">{c.detail}</td></tr>
              ))}</tbody></table></details>
          )}
          {Object.keys(latestExec.before_state).length > 0 && (
            <details><summary className="small">Before / after</summary>
              <pre className="block">{JSON.stringify({ before: latestExec.before_state, after: latestExec.after_state }, null, 1)}</pre></details>
          )}
          {latestExec.verification.rounds && (
            <details><summary className="small">Verification ({latestExec.verification.rounds.length} checks)</summary>
              <ul className="small">{latestExec.verification.rounds.map((r) => (
                <li key={r.at}>{fmtTime(r.at)} — {r.healthy ? "healthy" : "unhealthy"}: {r.checks.join("; ")}</li>
              ))}</ul></details>
          )}
        </div>
      )}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

export function IncidentDetailPage({ id }: { id: string }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [cited, setCited] = useState<Set<string>>(new Set());
  const opts = { refetchInterval: 5000 } as const;
  const detail = useQuery({ queryKey: ["incident", id, "detail"], queryFn: () => api.incident(id), ...opts });
  const timeline = useQuery({ queryKey: ["incident", id, "timeline"], queryFn: () => api.timeline(id), ...opts });
  const invs = useQuery({ queryKey: ["incident", id, "investigations"], queryFn: () => api.investigations(id), ...opts });
  const evidence = useQuery({ queryKey: ["incident", id, "evidence"], queryFn: () => api.evidence(id), ...opts });
  const approvals = useQuery({ queryKey: ["incident", id, "approvals"], queryFn: () => api.approvals(id), ...opts });
  const executions = useQuery({ queryKey: ["incident", id, "executions"], queryFn: () => api.executions(id), ...opts });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system });
  const act = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSettled: () => qc.invalidateQueries({ queryKey: ["incident", id] }),
  });

  if (detail.isLoading) return <div className="empty">Loading…</div>;
  if (detail.isError || !detail.data) return <div className="error">Could not load incident: {String(detail.error)}</div>;
  const inc = detail.data.incident;
  const inv = invs.data?.[0];
  const history = (timeline.data ?? []).filter((t) => t.kind === "transition").map((t) => t.title.split(" → ")[1] ?? "");
  const latestEvidence = (evidence.data ?? []).filter((e) => !inv || e.investigation_id === inv.id);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>{inc.title}</h1>
          <div className="row small secondary">
            <StatusBadge status={inc.status} /><SeverityBadge severity={inc.severity} />
            <span>service <strong>{inc.service}</strong></span>
            <span className="mono">{inc.fingerprint}</span>
            <span>first seen {fmtDateTime(inc.first_seen)} ({since(inc.first_seen)} ago)</span>
            <span>alert {inc.alert_active ? "firing" : "cleared"} · {inc.alert_count} notification(s)</span>
          </div>
        </div>
        <div className="row">
          {["OPEN", "NEEDS_HUMAN", "REJECTED", "FAILED"].includes(inc.status) && (
            <button className="btn" disabled={!can("investigate") || act.isPending}
              onClick={() => act.mutate(() => api.investigate(id))}>Re-investigate</button>
          )}
          {["NEEDS_HUMAN", "REJECTED", "FAILED"].includes(inc.status) && (
            <button className="btn" disabled={!can("investigate") || act.isPending}
              onClick={() => act.mutate(() => api.resolve(id, "resolved from dashboard"))}>Mark resolved</button>
          )}
        </div>
      </div>
      <div className="card"><Lifecycle status={inc.status} history={history} /></div>
      <ServiceCharts service={inc.service} window="1h" />
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="stack">
          {inv ? <Diagnosis inv={inv} onCite={(ids) => setCited(new Set(ids))} /> :
            <div className="card empty">No investigation yet.</div>}
          <EvidenceTable evidence={latestEvidence} grafana={system.data?.grafana_url ?? "http://127.0.0.1:13000"} highlight={cited} />
        </div>
        <div className="stack">
          <Remediation incidentId={id} status={inc.status} inv={inv?.status === "proposed" ? inv : undefined}
            approvals={approvals.data ?? []} executions={executions.data ?? []} />
          {inv && <AgentActivity inv={inv} />}
          <div className="card">
            <h2>Timeline</h2>
            <ol className="timeline">
              {[...(timeline.data ?? [])].reverse().map((t, i) => (
                <li key={i}><time>{fmtTime(t.at)}</time>
                  <div><strong className="small">{t.title}</strong> <span className="muted small">{t.actor}</span>
                    {typeof t.details.reason === "string" && <div className="small secondary">{t.details.reason}</div>}</div></li>
              ))}
            </ol>
            {inc.resolved_at && <p className="small muted">Resolved after {fmtDuration((new Date(inc.resolved_at).getTime() - new Date(inc.first_seen).getTime()) / 1000)}</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
