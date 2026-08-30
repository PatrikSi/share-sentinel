export type FindingSeverity = "critical" | "high" | "medium" | "low" | "info";
export type FindingStatus = "open" | "acknowledged" | "accepted_risk" | "resolved";
export type EvidenceState = "exact" | "bounded" | "indeterminate";

export type FindingEvidence = {
  state: EvidenceState;
  summary?: Record<string, unknown> | null;
  refs?: Record<string, unknown> | null;
  limitations?: string[] | null;
};

export type FindingEvidenceFact = {
  key: string;
  label: string;
  value: string;
  withheld: boolean;
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

export type FindingAssigneeCandidate = {
  id: string;
  email: string;
};

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

export function findingExpectedRevisions(
  findings: ReadonlyArray<Pick<Finding, "id" | "revision">>,
  selectedIds: ReadonlySet<string>,
): Record<string, number> {
  const expectedRevisions: Record<string, number> = {};
  findings.forEach((finding) => {
    if (!selectedIds.has(finding.id)) return;
    if (!Number.isSafeInteger(finding.revision) || finding.revision < 1) {
      throw new Error("A selected finding has an invalid revision and cannot be updated safely.");
    }
    expectedRevisions[finding.id] = finding.revision;
  });
  if (Object.keys(expectedRevisions).length !== selectedIds.size) {
    throw new Error("The selected findings no longer match the loaded page. Refresh and select them again.");
  }
  return expectedRevisions;
}

export function evidenceTrustCopy(state: EvidenceState): string {
  if (state === "exact") return "Exact within the declared collection scope.";
  if (state === "bounded") return "Based on bounded observations; review limitations before acting.";
  return "Collection evidence is insufficient for a definitive conclusion.";
}

const SAFE_EVIDENCE_TEXT_FIELDS = new Set([
  "access_state",
  "change_type",
  "content_state",
  "exposure",
  "match_quality",
  "method",
  "outcome",
  "probe_method",
  "provider",
  "scope",
  "state",
  "status",
  "structural_state",
]);
const SAFE_EVIDENCE_LIST_FIELDS = new Set(["allowed_capabilities", "capabilities", "categories", "change_categories"]);
const SAFE_EVIDENCE_REFERENCE_FIELDS = new Set(["comparison_id", "finding_id", "resource_id", "run_id"]);
const SAFE_EVIDENCE_CONTAINER_FIELDS = new Set(["after", "before", "positive_evidence"]);
const SENSITIVE_EVIDENCE_FIELD = /(^|_)(authorization|cookie|credential|password|secret|token|private_key|client_secret|access_key)($|_)/i;

function evidenceLabel(key: string): string {
  return humanizeMonitoringValue(key);
}

function boundedText(value: unknown, maxLength = 160): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1)}…`;
}

/**
 * Convert collector-controlled evidence into a small, conservative display model.
 * Only schema-known operational fields are rendered verbatim. Unknown structured
 * values remain acknowledged without exposing their contents, because snapshots
 * and provider metadata may contain sensitive material.
 */
export function findingEvidenceFacts(raw: unknown, kind: "summary" | "references" = "summary", maxFacts = 16): FindingEvidenceFact[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return raw == null
      ? []
      : [{
          key: kind,
          label: kind === "references" ? "References" : "Summary",
          value: "Legacy evidence was recorded; raw content is withheld.",
          withheld: true,
        }];
  }

  return Object.entries(raw as Record<string, unknown>).slice(0, Math.max(1, maxFacts)).map(([key, value]) => {
    if (SENSITIVE_EVIDENCE_FIELD.test(key)) {
      return { key, label: "Sensitive field", value: "Sensitive value withheld", withheld: true };
    }

    const knownField = kind === "references"
      ? SAFE_EVIDENCE_REFERENCE_FIELDS.has(key)
      : SAFE_EVIDENCE_TEXT_FIELDS.has(key) || SAFE_EVIDENCE_LIST_FIELDS.has(key) || SAFE_EVIDENCE_CONTAINER_FIELDS.has(key) || key === "complete";
    const label = knownField ? evidenceLabel(key) : "Additional evidence";

    if (kind === "references" && SAFE_EVIDENCE_REFERENCE_FIELDS.has(key) && ["string", "number"].includes(typeof value)) {
      return { key, label, value: boundedText(value, 80), withheld: false };
    }
    if (kind === "summary" && SAFE_EVIDENCE_TEXT_FIELDS.has(key) && ["string", "number", "boolean"].includes(typeof value)) {
      return { key, label, value: boundedText(value), withheld: false };
    }
    if (kind === "summary" && key === "complete" && typeof value === "boolean") {
      return { key, label, value: value ? "Yes" : "No", withheld: false };
    }
    if (kind === "summary" && SAFE_EVIDENCE_LIST_FIELDS.has(key) && Array.isArray(value)) {
      const safeValues = value.filter((entry): entry is string => typeof entry === "string").slice(0, 12);
      const omitted = Math.max(0, value.length - safeValues.length);
      return {
        key,
        label,
        value: safeValues.length ? `${safeValues.map((entry) => boundedText(entry, 80)).join(", ")}${omitted ? ` (+${omitted} more)` : ""}` : "None recorded",
        withheld: safeValues.length !== value.length,
      };
    }

    if (Array.isArray(value)) {
      return { key, label, value: `${value.length.toLocaleString()} values recorded; raw values withheld`, withheld: true };
    }
    if (value && typeof value === "object") {
      const count = Object.keys(value as Record<string, unknown>).length;
      return { key, label, value: `${count.toLocaleString()} fields recorded; raw values withheld`, withheld: true };
    }
    return { key, label, value: "Recorded; raw value withheld", withheld: true };
  });
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
