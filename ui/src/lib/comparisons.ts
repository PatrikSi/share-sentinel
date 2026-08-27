import type { AccessEvidenceSummary } from "@/lib/access-evidence";

export type ComparisonState = "queued" | "running" | "complete" | "failed";
export type ResourceChangeType = "appeared" | "disappeared" | "changed" | "indeterminate";

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
  appeared: number;
  disappeared: number;
  changed: number;
  indeterminate: number;
  total: number;
  exact: boolean;
  resource_summary_exact?: boolean;
  item_churn_computed?: boolean;
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
  state: string;
  added: number | null;
  removed: number | null;
  moved: number | null;
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

export type ComparisonTone = "success" | "warning" | "error" | "info";

export function canCreateMaterializedComparison(role: string | null | undefined, roleStatus: string): boolean {
  const normalizedRole = role?.trim().toLowerCase();
  return roleStatus === "ready" && (normalizedRole === "operator" || normalizedRole === "admin");
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
  if (itemChanges.state === "indeterminate" || itemChanges.state === "partial") {
    return "Item-level changes are indeterminate because collection coverage was incomplete.";
  }
  const added = itemChanges.added == null ? "unknown" : itemChanges.added.toLocaleString();
  const removed = itemChanges.removed == null ? "unknown" : itemChanges.removed.toLocaleString();
  const moved = itemChanges.moved == null ? "unknown" : itemChanges.moved.toLocaleString();
  return `${added} added · ${removed} removed · ${moved} moved or renamed`;
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
