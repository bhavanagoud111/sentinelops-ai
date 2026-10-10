// Status and severity badges: always icon/shape + text label, never color alone.
import type { IncidentStatus } from "../api";

type Tone = "good" | "warning" | "serious" | "critical" | "neutral" | "info";
const TONE_COLOR: Record<Tone, string> = {
  good: "var(--good)", warning: "var(--warning)", serious: "var(--serious)",
  critical: "var(--critical)", neutral: "var(--text-muted)", info: "var(--accent)",
};
const TONE_ICON: Record<Tone, string> = {
  good: "✓", warning: "!", serious: "▲", critical: "✕", neutral: "•", info: "◆",
};

export function Badge({ tone, label, title }: { tone: Tone; label: string; title?: string }) {
  return (
    <span className="badge" title={title}>
      <span aria-hidden="true" style={{ color: TONE_COLOR[tone], fontWeight: 800 }}>{TONE_ICON[tone]}</span>
      {label}
    </span>
  );
}

const STATUS_TONE: Record<IncidentStatus, Tone> = {
  OPEN: "serious", TRIAGING: "info", INVESTIGATING: "info", AWAITING_APPROVAL: "warning",
  REMEDIATING: "info", VERIFYING: "info", RESOLVED: "good", NEEDS_HUMAN: "serious",
  REJECTED: "neutral", FAILED: "critical",
};
const STATUS_LABEL: Record<IncidentStatus, string> = {
  OPEN: "Open", TRIAGING: "Triaging", INVESTIGATING: "Investigating",
  AWAITING_APPROVAL: "Awaiting approval", REMEDIATING: "Remediating", VERIFYING: "Verifying",
  RESOLVED: "Resolved", NEEDS_HUMAN: "Needs human", REJECTED: "Rejected", FAILED: "Failed",
};
export const statusLabel = (s: IncidentStatus) => STATUS_LABEL[s] ?? s;

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"} label={statusLabel(status)} />;
}

const SEV_TONE: Record<string, Tone> = { critical: "critical", high: "serious", warning: "warning", info: "neutral" };
export function SeverityBadge({ severity }: { severity: string }) {
  return <Badge tone={SEV_TONE[severity] ?? "neutral"} label={severity} />;
}

export function VerdictBadge({ verdict }: { verdict: string }) {
  const tone: Tone = verdict === "accepted" ? "good" : verdict === "downgraded" ? "warning" : "critical";
  return <Badge tone={tone} label={verdict} />;
}

export function Confidence({ value }: { value: number }) {
  return (
    <span className="row small" title={`confidence ${value.toFixed(2)}`}>
      <span style={{ display: "inline-block", width: 64, height: 6, background: "var(--surface-2)",
        borderRadius: 3, overflow: "hidden" }} aria-hidden="true">
        <span style={{ display: "block", width: `${Math.round(value * 100)}%`, height: "100%",
          background: "var(--series-1)", borderRadius: 3 }} />
      </span>
      <span className="mono">{value.toFixed(2)}</span>
    </span>
  );
}
