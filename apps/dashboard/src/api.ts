// Typed client for the SentinelOps API. All requests carry the user's bearer token.

export type IncidentStatus =
  | "OPEN" | "TRIAGING" | "INVESTIGATING" | "AWAITING_APPROVAL" | "REMEDIATING"
  | "VERIFYING" | "RESOLVED" | "NEEDS_HUMAN" | "REJECTED" | "FAILED";

export interface Incident {
  id: string; fingerprint: string; title: string; status: IncidentStatus;
  severity: "critical" | "high" | "warning" | "info"; service: string; environment: string;
  alert_active: boolean; alert_count: number; first_seen: string; last_seen: string;
  resolved_at: string | null; summary: string; version: number;
}
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface ServiceInfo {
  name: string; namespace: string; owner: string; criticality: string; description: string;
  dependencies: string[];
}
export interface AlertEventOut {
  event_id: string; status: string; received_at: string; alertname: string; severity: string;
  starts_at: string; labels: Record<string, string>; annotations: Record<string, string>;
}
export interface IncidentDetail { incident: Incident; alerts: AlertEventOut[]; service: ServiceInfo | null }
export interface TimelineEntry {
  at: string; kind: string; title: string; actor: string | null; details: Record<string, unknown>;
}
export interface Hypothesis {
  key: string; rank: number; cause_category: string; suspected_service: string;
  description: string; confidence: number; adjusted_confidence: number; verdict: string;
  critic_reasons: string[]; evidence_ids: string[]; counterevidence_ids: string[];
  recommended_runbook_id: string | null; runbook_params: Record<string, unknown>;
  uncertainty: string; next_checks: string[];
}
export interface AgentStep {
  node: string; started_at: string; finished_at: string; summary: string;
  tool_calls: Record<string, unknown>[]; model: string | null; usage: Record<string, number>;
}
export interface Proposal {
  runbook_id: string; params: Record<string, unknown>; risk: string; hypothesis_id: string;
  rationale: string; evidence_ids: string[]; blast_radius: Record<string, unknown>;
  target_revision: number | null;
}
export interface Investigation {
  id: string; status: string; provider: string; trigger: string; started_at: string;
  finished_at: string | null; summary: string; triage: Record<string, unknown>;
  steps: AgentStep[]; proposal: Proposal | null; usage: Record<string, number>;
  error: string | null; hypotheses: Hypothesis[];
}
export interface Evidence {
  evidence_id: string; investigation_id: string; kind: string; service: string; source: string;
  tool: string; query: string; range_start: string; range_end: string; retrieved_at: string;
  summary: string; anomalous: boolean; signals: Record<string, unknown>; content_hash: string;
  error: string | null;
}
export interface Approval {
  id: string; incident_id: string; runbook_id: string; runbook_title: string;
  params: Record<string, unknown>; rationale: string; risk: string; target_revision: number | null;
  status: string; requested_by: string; requested_at: string; approved_by: string | null;
  decided_at: string | null; decision_reason: string | null; expires_at: string; request_hash: string;
}
export interface PolicyCheck { name: string; passed: boolean; detail: string }
export interface Execution {
  id: string; incident_id: string; approval_id: string; runbook_id: string; target: string;
  params: Record<string, unknown>; status: string; requested_by: string;
  policy: { allowed?: boolean; checks?: PolicyCheck[] }; command_template: string;
  before_state: Record<string, unknown>; after_state: Record<string, unknown>;
  verification: { recovered?: boolean; rounds?: { at: string; healthy: boolean; checks: string[] }[] };
  error: string | null; created_at: string; started_at: string | null; finished_at: string | null;
}
export interface ServiceMetrics {
  service: string; window: string; step_s: number;
  series: { metric: string; points: [number, number][] }[];
}
export interface Me {
  access_token: string; subject: string; name: string; roles: string[]; permissions: string[];
}
export interface DevUser { username: string; name: string; roles: string[] }
export interface Stats {
  incidents_by_status: Record<string, number>; verified_recoveries: number;
  mttr_seconds_p50: number | null; executions_by_status: Record<string, number>;
}
export interface SystemStatus {
  integrations: Record<string, boolean>; remediation_mode: string; llm_provider: string;
  grafana_url: string; stream_subscribers: number; dev_login: boolean;
}
export interface Scenario {
  id: string; title: string; description: string; expected_alert: string; expected_outcome: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

const TOKEN_KEY = "sentinelops.token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable: session-only login */ }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error */ }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const idem = () => ({ "Idempotency-Key": crypto.randomUUID() });
const post = <T>(path: string, body: unknown = {}, headers: HeadersInit = {}) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body), headers });

export const api = {
  devUsers: () => request<DevUser[]>("/api/v1/auth/dev-users"),
  devLogin: (username: string) => post<Me>("/api/v1/auth/dev-token", { username }),
  me: () => request<Me>("/api/v1/auth/me"),
  incidents: (params: { status?: string[]; service?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    params.status?.forEach((s) => q.append("status", s));
    if (params.service) q.set("service", params.service);
    q.set("limit", String(params.limit ?? 100));
    return request<Page<Incident>>(`/api/v1/incidents?${q}`);
  },
  incident: (id: string) => request<IncidentDetail>(`/api/v1/incidents/${id}`),
  timeline: (id: string) => request<TimelineEntry[]>(`/api/v1/incidents/${id}/timeline`),
  investigations: (id: string) => request<Investigation[]>(`/api/v1/incidents/${id}/investigations`),
  evidence: (id: string) => request<Evidence[]>(`/api/v1/incidents/${id}/evidence`),
  approvals: (id: string) => request<Approval[]>(`/api/v1/incidents/${id}/approvals`),
  executions: (id: string) => request<Execution[]>(`/api/v1/incidents/${id}/executions`),
  services: () => request<ServiceInfo[]>("/api/v1/services"),
  metrics: (name: string, window: string) =>
    request<ServiceMetrics>(`/api/v1/services/${name}/metrics?window=${window}`),
  stats: () => request<Stats>("/api/v1/stats"),
  system: () => request<SystemStatus>("/api/v1/system/status"),
  scenarios: () => request<Scenario[]>("/api/v1/experiments"),
  investigate: (id: string) => post(`/api/v1/incidents/${id}/investigate`, {}, idem()),
  resolve: (id: string, reason: string) => post(`/api/v1/incidents/${id}/resolve`, { reason }),
  requestApproval: (id: string) => post<Approval>(`/api/v1/incidents/${id}/approvals`, {}, idem()),
  decide: (approvalId: string, decision: "approve" | "reject", reason: string) =>
    post<Approval>(`/api/v1/approvals/${approvalId}/decision`, { decision, reason }),
  execute: (id: string, runbook: string, approvalId: string) =>
    post<Execution>(`/api/v1/incidents/${id}/actions/${runbook}/execute`,
      { approval_id: approvalId }, idem()),
  startScenario: (sid: string) => post(`/api/v1/experiments/${sid}`),
  resetScenarios: () => post("/api/v1/experiments/reset"),
};
