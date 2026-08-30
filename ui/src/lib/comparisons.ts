import type { AccessEvidenceSummary } from "@/lib/access-evidence";
import {
  canManageFindings,
  monitoringEvaluationState,
  type MonitoringEvaluation,
} from "@/lib/monitoring";

export type ComparisonState = "queued" | "running" | "complete" | "failed";
export type ResourceChangeType = "appeared" | "disappeared" | "changed" | "indeterminate";
export type ItemChangeType = "added" | "removed" | "moved" | "renamed" | "metadata_changed" | "permission_changed" | "indeterminate";

export type ComparisonRun = {
  id: string;
  name?: string | null;
  created_at?: string | null;
  status?: string | null;
};

export type ComparisonCompatibility = {
  status: string;
  structural_interpretable: boolean;
  content_interpretable: boolean;
  access_context_comparable?: boolean;
  access_interpretable: boolean;
  capability_applicable?: boolean;
  capability_interpretable?: boolean;
  identity_applicable?: boolean;
  identity_scope_exact?: boolean;
  direct_permissions_assessed?: boolean;
  direct_permissions_complete?: boolean;
  direct_permissions_interpretable?: boolean;
  direct_permissions_scope_exact?: boolean;
  reasons: string[];
};

export type ComparisonSummary = {
  appeared?: number | null;
  disappeared?: number | null;
  changed?: number | null;
  indeterminate?: number | null;
  total?: number | null;
  exact?: boolean;
  resource_summary_exact?: boolean;
  item_churn_computed?: boolean;
  findings_evaluation?: MonitoringEvaluation | null;
  [key: string]: unknown;
};

export type ComparisonSummaryCounts = {
  appeared: number;
  disappeared: number;
  changed: number;
  indeterminate: number;
  total: number;
  published: boolean;
};

export type ProjectComparison = {
  id: string;
  project_id?: string | null;
  state: ComparisonState;
  current_run?: ComparisonRun | null;
  baseline_run?: ComparisonRun | null;
  current_run_id?: string | null;
  baseline_run_id?: string | null;
  compatibility?: ComparisonCompatibility | null;
  summary?: ComparisonSummary | null;
  progress?: {
    processed?: number | null;
    total?: number | null;
    message?: string | null;
  } | null;
  error?: string | { code?: string | null; message?: string | null } | null;
  created_at?: string | null;
  completed_at?: string | null;
  next_retry_at?: string | null;
  updated_at?: string | null;
};

export type ResourceComparisonSnapshot = {
  resource_id?: number | null;
  run_id?: string | null;
  name?: string | null;
  endpoint_key?: string | null;
  hostname?: string | null;
  provider?: string | null;
  resource_type?: string | null;
  provider_resource_id?: string | null;
  access_level?: string | null;
  access_evidence_summary?: AccessEvidenceSummary | null;
  permission_summary?: AccessEvidenceSummary | null;
  exposure?: string | null;
  lifecycle_state?: string | null;
  item_count?: number | null;
  file_count?: number | null;
  folder_count?: number | null;
  total_size_bytes?: number | null;
};

export type ResourceItemChanges = {
  state: "computed" | "not_computed";
  exact: boolean;
  counts: Partial<Record<ItemChangeType, number>> | null;
  total: number | null;
  before_count: number | null;
  after_count: number | null;
};

export type ResourceComparisonChange = {
  id?: string | number | null;
  resource_key?: string | null;
  identity_key?: string | null;
  change_type: ResourceChangeType;
  provider?: string | null;
  resource_type?: string | null;
  provider_resource_id?: string | null;
  change_categories?: string[];
  categories?: string[];
  structural_state: string;
  access_state: string;
  content_state: string;
  access_interpretation?: string | null;
  match?: {
    basis?: string | null;
    quality?: string | null;
  } | null;
  before?: ResourceComparisonSnapshot | null;
  after?: ResourceComparisonSnapshot | null;
  item_changes?: ResourceItemChanges | null;
};

export type ResourceChangePage = {
  items: ResourceComparisonChange[];
  next_cursor: string | null;
};

export type ItemComparisonSnapshot = {
  id?: number | null;
  item_id?: number | null;
  resource_id?: number | null;
  path?: string | null;
  name?: string | null;
  is_dir?: boolean | null;
  size_bytes?: number | null;
  mtime?: string | null;
  provider_item_id?: string | null;
  resource_name?: string | null;
  endpoint_key?: string | null;
  [key: string]: unknown;
};

export type ItemComparisonChange = {
  id: number;
  resource_change_id?: number | null;
  identity_key: string;
  change_type: ItemChangeType;
  provider?: string | null;
  before?: ItemComparisonSnapshot | null;
  after?: ItemComparisonSnapshot | null;
  change_categories?: string[];
  evidence_state: "exact" | "bounded" | "indeterminate";
  limitations?: string[];
  match?: { basis?: string | null; quality?: string | null } | null;
  impact_rank?: number | null;
};

export type ItemChangePage = {
  items: ItemComparisonChange[];
  next_cursor: string | null;
  comparison_state: ComparisonState;
  interpretation?: {
    state?: string | null;
    exact?: boolean;
    limitations?: string[];
  } | null;
};

export type ComparisonTone = "success" | "warning" | "error" | "info";

export function canCreateMaterializedComparison(role: string | null | undefined, roleStatus: string): boolean {
  const normalizedRole = role?.trim().toLowerCase();
  return roleStatus === "ready" && (normalizedRole === "operator" || normalizedRole === "admin");
}

function safeSummaryCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function comparisonSummaryCounts(
  summary: ComparisonSummary | null | undefined,
): ComparisonSummaryCounts {
  const appeared = safeSummaryCount(summary?.appeared);
  const disappeared = safeSummaryCount(summary?.disappeared);
  const changed = safeSummaryCount(summary?.changed);
  const indeterminate = safeSummaryCount(summary?.indeterminate);
  const total = safeSummaryCount(summary?.total);
  const published = [appeared, disappeared, changed, indeterminate, total].every((value) => value !== null);
  return {
    appeared: appeared ?? 0,
    disappeared: disappeared ?? 0,
    changed: changed ?? 0,
    indeterminate: indeterminate ?? 0,
    total: total ?? 0,
    published,
  };
}

export function emptyResourceChangesDescription(
  summary: ComparisonSummary | null | undefined,
  filtersApplied: boolean,
): string {
  if (filtersApplied) return "No resource changes match the current server-side filters.";
  const resourceCopy = "No resource-level changes were published within the comparable collected scope.";
  if (summary?.item_churn_computed === true) {
    return `${resourceCopy} Item history was computed independently; open the Item history tab for its result.`;
  }
  if (summary?.item_churn_computed === false) {
    return `${resourceCopy} Item history was not computed for this comparison.`;
  }
  return `${resourceCopy} Item-history computation state was not recorded.`;
}

export function comparisonFindingsEvaluation(
  comparison: ProjectComparison | null | undefined,
): MonitoringEvaluation | null {
  const evaluation = comparison?.summary?.findings_evaluation;
  return evaluation && typeof evaluation === "object" ? evaluation : null;
}

export function canRetryComparisonFindings(
  comparison: ProjectComparison | null | undefined,
  role: string | null | undefined,
): boolean {
  return canManageFindings(role)
    && comparison?.state === "complete"
    && monitoringEvaluationState(comparisonFindingsEvaluation(comparison)) === "degraded";
}

export function canRetryMaterializedComparison(
  comparison: ProjectComparison | null | undefined,
  role: string | null | undefined,
): boolean {
  const normalizedRole = role?.trim().toLowerCase();
  return comparison?.state === "failed" && (normalizedRole === "operator" || normalizedRole === "admin");
}

export function comparisonRunId(comparison: ProjectComparison, side: "current" | "baseline"): string | null {
  return side === "current"
    ? comparison.current_run?.id || comparison.current_run_id || null
    : comparison.baseline_run?.id || comparison.baseline_run_id || null;
}

export function comparisonRunLabel(comparison: ProjectComparison, side: "current" | "baseline"): string {
  const run = side === "current" ? comparison.current_run : comparison.baseline_run;
  const id = comparisonRunId(comparison, side);
  return run?.name?.trim() || id || (side === "current" ? "Current run" : "Baseline run");
}

export function comparisonStateTone(state: ComparisonState): ComparisonTone {
  if (state === "complete") return "success";
  if (state === "failed") return "error";
  return "info";
}

export function comparisonErrorText(error: ProjectComparison["error"]): string | null {
  if (!error) return null;
  if (typeof error === "string") return error;
  if (error.message && error.code) return `${error.message} (${error.code})`;
  return error.message || error.code || "Comparison failed without a recorded reason.";
}

export function comparisonCompatibilityTone(compatibility: ComparisonCompatibility | null | undefined): ComparisonTone {
  if (!compatibility) return "warning";
  const capabilitiesSatisfied = compatibility.capability_applicable === false
    || compatibility.capability_interpretable === true;
  const identitySatisfied = compatibility.identity_applicable === false
    || compatibility.identity_scope_exact !== false;
  if (
    compatibility.structural_interpretable &&
    compatibility.content_interpretable &&
    compatibility.access_interpretable &&
    identitySatisfied &&
    capabilitiesSatisfied &&
    compatibility.direct_permissions_interpretable === true
  ) {
    return "success";
  }
  return compatibility.structural_interpretable ? "warning" : "error";
}

export function changeSnapshot(change: ResourceComparisonChange): ResourceComparisonSnapshot | null {
  return change.after || change.before || null;
}

export function resourceChangeName(change: ResourceComparisonChange): string {
  return change.after?.name || change.before?.name || "Unnamed resource";
}

export function resourceChangeProvider(change: ResourceComparisonChange): string {
  return change.provider || change.after?.provider || change.before?.provider || "unknown";
}

export function resourceChangeKey(change: ResourceComparisonChange, fallbackIndex = 0): string {
  if (change.id !== null && change.id !== undefined) return String(change.id);
  if (change.resource_key || change.identity_key) return change.resource_key || change.identity_key || String(fallbackIndex);
  const snapshot = changeSnapshot(change);
  return [
    change.change_type,
    resourceChangeProvider(change),
    snapshot?.endpoint_key || "no-endpoint",
    snapshot?.provider_resource_id || snapshot?.resource_id || resourceChangeName(change),
    fallbackIndex,
  ].join(":");
}

export function resourceChangeCategories(change: ResourceComparisonChange): string[] {
  return Array.isArray(change.change_categories)
    ? change.change_categories
    : Array.isArray(change.categories)
      ? change.categories
      : [];
}

export function itemChangeCopy(itemChanges: ResourceItemChanges | null | undefined): string {
  if (!itemChanges || itemChanges.state === "not_computed") {
    return "Item-level changes were not computed for this scalable comparison.";
  }
  if (!itemChanges.counts) {
    return "Computed item history did not publish usable counts; treat this result as indeterminate.";
  }
  const count = (type: ItemChangeType): string => {
    const value = itemChanges.counts?.[type];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? value.toLocaleString()
      : "unknown";
  };
  const parts = [
    `${count("added")} added`,
    `${count("removed")} removed`,
    `${count("moved")} moved`,
    `${count("renamed")} renamed`,
  ];
  for (const [type, label] of [
    ["metadata_changed", "metadata changed"],
    ["permission_changed", "permission changed"],
    ["indeterminate", "indeterminate"],
  ] as const) {
    const value = itemChanges.counts[type];
    if (typeof value === "number" && value > 0) parts.push(`${value.toLocaleString()} ${label}`);
  }
  if (!itemChanges.exact) parts.push("bounded evidence");
  return parts.join(" · ");
}

export function changeTypeLabel(changeType: ResourceChangeType): string {
  if (changeType === "appeared") return "Appeared";
  if (changeType === "disappeared") return "Disappeared";
  if (changeType === "indeterminate") return "Indeterminate";
  return "Changed";
}

export function changeTypeTone(changeType: ResourceChangeType): "positive" | "negative" | "warning" | "neutral" {
  if (changeType === "appeared") return "positive";
  if (changeType === "disappeared") return "negative";
  if (changeType === "indeterminate") return "warning";
  return "neutral";
}

export function normalizeChangeType(value: string | null | undefined): ResourceChangeType | "all" {
  return value === "appeared" || value === "disappeared" || value === "changed" || value === "indeterminate"
    ? value
    : "all";
}
