export type FindingSeverity = "critical" | "high" | "medium" | "low" | "info";
export type FindingStatus = "open" | "acknowledged" | "accepted_risk" | "resolved";
export type EvidenceState = "exact" | "bounded" | "indeterminate";

export type FindingEvidence = {
  state: EvidenceState;
  summary?: string | null;
  refs?: Array<Record<string, unknown> | string> | null;
  limitations?: string[] | null;
};

export type Finding = {
  id: string;
  project_id: string;
  source_id?: string | null;
  policy_id: string;
  policy_version: number;
  title: string;
  description?: string | null;
  severity: FindingSeverity;
  status: FindingStatus;
  resource_identity_key?: string | null;
  resource_type?: string | null;
  provider?: string | null;
  resource_name?: string | null;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at?: string | null;
  accepted_risk_expires_at?: string | null;
  assignee_user_id?: string | null;
  latest_run_id?: string | null;
  latest_comparison_id?: string | null;
  evidence: FindingEvidence;
  occurrence_count: number;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type FindingSummary = Record<FindingStatus, number> & { total: number };

export type FindingPolicy = {
  id: string;
  version: number;
  title: string;
  description?: string | null;
  severity: FindingSeverity;
  category?: string | null;
  enabled: boolean;
  evidence_requirements?: string[] | Record<string, unknown> | null;
};

export type FindingOccurrence = {
  id: number;
  run_id: string;
  comparison_id?: string | null;
  policy_id: string;
  policy_version: number;
  evidence_state: EvidenceState;
  evidence?: FindingEvidence | Record<string, unknown> | null;
  observed_at: string;
};

export type FindingActivity = {
  id: number;
  ts: string;
  action: string;
  actor_user_id?: string | null;
  actor_token_id?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type SourceCoverage = {
  state: "complete" | "partial" | "unknown";
  reasons?: string[] | null;
};

export type SourceFreshness = {
  state: "fresh" | "stale" | "unknown" | "disabled";
  age_seconds?: number | null;
  expected_interval_seconds?: number | null;
  stale_after_seconds?: number | null;
};

export type SourceHealth = "healthy" | "stale" | "degraded" | "never_collected" | "disabled";

export type MonitoringSource = {
  id: string;
  project_id: string;
  source_key: string;
  display_name: string;
  provider: string;
  assessed_identity?: string | null;
  target_scope?: Record<string, unknown> | null;
  enabled: boolean;
  expected_interval_seconds?: number | null;
  last_run_id?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_comparison_id?: string | null;
  collector_version?: string | null;
  coverage: SourceCoverage;
  freshness: SourceFreshness;
  health_status: SourceHealth;
  health_reasons?: string[] | null;
  created_at: string;
  updated_at: string;
};

export type EffectiveAccessPrincipal = {
  id?: string | number | null;
  display_name?: string | null;
  principal_type?: string | null;
  kind?: string | null;
  provider_principal_id?: string | null;
  principal_key?: string | null;
  login_name?: string | null;
  email?: string | null;
  resolution?: string | null;
  direct_decision?: string | null;
  effective_decision?: string | null;
  decision?: string | null;
  capabilities?: string[] | Record<string, unknown> | null;
  allow_rights?: string[] | null;
  deny_rights?: string[] | null;
  limitations?: string[] | null;
  explanation?: string | null;
  paths?: string[] | null;
  [key: string]: unknown;
};

export type EffectiveAccessAnalysis = {
  resource?: Record<string, unknown> | null;
  analysis_state: "computed" | "bounded" | "indeterminate";
  decision: "allowed" | "denied" | "mixed" | "unknown";
  capabilities?: string[] | Record<string, unknown> | null;
  principals: { items: EffectiveAccessPrincipal[]; next_cursor?: string | null };
  evidence_planes?: Record<string, unknown> | null;
  limitations?: string[] | null;
  provenance?: Record<string, unknown> | null;
};

export function humanizeMonitoringValue(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  return value
    .trim()
    .replaceAll("-", "_")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function findingSeverityRank(severity: FindingSeverity): number {
  return ({ critical: 5, high: 4, medium: 3, low: 2, info: 1 })[severity];
}

export function findingTone(severity: FindingSeverity): "critical" | "high" | "medium" | "low" | "info" {
  return severity;
}

export function canManageFindings(role: string | null | undefined): boolean {
  return role === "operator" || role === "admin";
}

export function canManageSources(role: string | null | undefined): boolean {
  return role === "admin";
}

export function evidenceTrustCopy(state: EvidenceState): string {
  if (state === "exact") return "Exact within the declared collection scope.";
  if (state === "bounded") return "Based on bounded observations; review limitations before acting.";
  return "Collection evidence is insufficient for a definitive conclusion.";
}

export function formatMonitoringTimestamp(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "Unknown";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
