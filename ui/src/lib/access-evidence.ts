export type AccessEvidenceTone = "neutral" | "positive" | "warning" | "negative";

export type DirectPermissionEvidenceSummary = {
  evidence_available?: boolean;
  status?: string | null;
  assessment_count?: number | null;
  entry_count?: number | null;
  comparable?: boolean;
  negative_conclusion_supported?: boolean;
  semantics?: string[] | null;
  permission_surfaces?: string[] | null;
  observed_at?: string | null;
  partial?: boolean;
};

export type CapabilityObservationSummary = {
  evidence_available?: boolean;
  attempted?: number | null;
  allowed?: string[] | null;
  denied?: string[] | null;
  inconclusive?: string[] | null;
  writable_observed?: boolean;
  complete?: boolean;
  partial?: boolean;
  method?: string | null;
};

export type AccessEvidenceSummary = {
  label?: string | null;
  detail?: string | null;
  outcome?: string | null;
  assessment_state?: string | null;
  coverage?: string | null;
  scope?: string | null;
  direct_permissions_assessed?: boolean;
  direct_assessment_available?: boolean;
  direct_permission_count?: number | null;
  partial?: boolean;
  tone?: AccessEvidenceTone | null;
  status?: string | null;
  evidence_available?: boolean;
  assessment_count?: number | null;
  entry_count?: number | null;
  comparable?: boolean;
  negative_conclusion_supported?: boolean;
  semantics?: string[] | null;
  permission_surfaces?: string[] | null;
  observed_at?: string | null;
  access_level?: string | null;
  access_capabilities?: Record<string, unknown> | null;
  exposure?: string | null;
  exposure_evidence?: Record<string, unknown> | null;
  direct_permissions?: DirectPermissionEvidenceSummary | null;
  capability_observations?: CapabilityObservationSummary | null;
  compatibility_access_level?: string | null;
};

export type AccessEvidenceClaim = {
  id?: string | number | null;
  kind?: string | null;
  capability?: string | null;
  label?: string | null;
  outcome?: string | null;
  status?: string | null;
  value?: string | number | boolean | null;
  detail?: string | null;
  reason?: string | null;
  reason_code?: string | null;
  protocol_status?: string | null;
  method?: string | null;
  scope?: string | null;
};

export type AccessEvidencePrincipal = {
  id?: string | null;
  provider_principal_id?: string | null;
  provider?: string | null;
  identifier_namespace?: string | null;
  authority?: string | null;
  display_name?: string | null;
  name?: string | null;
  email?: string | null;
  principal_type?: string | null;
  type?: string | null;
  kind?: string | null;
  native_id?: string | null;
  login_name?: string | null;
  principal_key?: string | null;
  resolution?: string | null;
  resolution_source?: string | null;
  aliases?: string[] | null;
};

export type DirectPermissionEntry = {
  id?: string | number | null;
  entry_key?: string | null;
  provider_entry_id?: string | null;
  ordinal?: number | null;
  principal?: AccessEvidencePrincipal | null;
  principal_id?: string | null;
  principal_display_name?: string | null;
  roles?: string[] | null;
  rights?: string[] | null;
  normalized_rights?: string[] | null;
  effect?: string | null;
  inherited?: boolean | null;
  link_scope?: string | null;
  expires_at?: string | null;
  expiration_at?: string | null;
  inherited_state?: string | null;
  source_permission_id?: string | null;
  detail?: string | null;
  entry_kind?: string | null;
  evidence_hash?: string | null;
  provider_details?: Record<string, unknown> | null;
};

export type AccessEvidenceProvenance = {
  provider?: string | null;
  collector_version?: string | null;
  artifact_schema_version?: number | null;
  method?: string | null;
  assessed_identity?: string | null;
  collected_at?: string | null;
  run_id?: string | null;
  source?: string | null;
  run_name?: string | null;
  run_created_at?: string | null;
  collection_context?: Record<string, unknown> | null;
  pagination?: {
    assessment_limit?: number | null;
    after_assessment_id?: number | null;
    assessments_truncated?: boolean;
    next_assessment_id?: number | null;
    entry_limit?: number | null;
    after_entry_id?: number | null;
    entries_truncated?: boolean;
    next_entry_id?: number | null;
  } | null;
  [key: string]: unknown;
};

export type AccessEvidenceAssessment = {
  id?: string | number | null;
  kind?: string | null;
  semantics?: string | null;
  provider?: string | null;
  permission_surface?: string | null;
  method?: string | null;
  subject?: {
    kind?: string | null;
    key?: string | null;
    provider_id?: string | null;
    path?: string | null;
    item_id?: number | null;
  } | null;
  label?: string | null;
  summary?: string | Record<string, unknown> | null;
  outcome?: string | null;
  assessment_state?: string | null;
  state?: string | null;
  scope?: string | null;
  coverage?: string | {
    selection_scope?: string | null;
    selection?: string | null;
    retrieval?: string | null;
    provider_visibility?: string | null;
    semantic?: string | null;
    principal_resolution?: string | null;
    effective_access?: string | null;
    negative_conclusion_supported?: boolean | null;
  } | null;
  counts?: {
    observed?: number | null;
    emitted?: number | null;
    omitted?: number | null;
    unknown?: number | null;
  } | null;
  authoritative?: boolean | null;
  claims?: AccessEvidenceClaim[] | null;
  entries?: DirectPermissionEntry[] | null;
  limitations?: string[] | null;
  errors?: Array<string | { code?: string | null; message?: string | null }> | null;
  error_code?: string | null;
  observed_at?: string | null;
  provider_details?: Record<string, unknown> | null;
  provenance?: AccessEvidenceProvenance | null;
};

export type AccessEvidenceResource = {
  id: number;
  run_id?: string | null;
  name?: string | null;
  provider?: string | null;
  resource_type?: string | null;
  endpoint_key?: string | null;
  provider_resource_id?: string | null;
  web_url?: string | null;
};

export type AccessEvidenceDetail = {
  resource?: AccessEvidenceResource | null;
  overall: AccessEvidenceSummary;
  assessments: AccessEvidenceAssessment[];
  provenance?: AccessEvidenceProvenance | null;
};

export type AccessEvidencePresentation = {
  label: string;
  detail: string;
  tone: AccessEvidenceTone;
  stateLabel: string;
  coverageLabel: string | null;
  directPermissionsLabel: string;
  directPermissionsAssessed: boolean;
};

export function humanizeEvidenceValue(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  return value
    .trim()
    .replaceAll("-", "_")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function evidenceTone(outcome: string | null | undefined, state?: string | null): AccessEvidenceTone {
  const normalizedOutcome = (outcome || "").toLowerCase();
  const normalizedState = (state || "").toLowerCase();
  if (normalizedState === "failed" || normalizedOutcome === "denied") return "negative";
  if (
    normalizedState === "partial" ||
    normalizedState === "in_progress" ||
    normalizedOutcome === "mixed" ||
    normalizedOutcome === "inconclusive"
  ) {
    return "warning";
  }
  if (normalizedOutcome === "observed" || normalizedOutcome === "allowed") return "positive";
  return "neutral";
}

function validTone(value: string | null | undefined): value is AccessEvidenceTone {
  return value === "neutral" || value === "positive" || value === "warning" || value === "negative";
}

function normalizedCapabilityNames(value: string[] | null | undefined): Set<string> {
  return new Set(
    (Array.isArray(value) ? value : [])
      .filter((entry): entry is string => typeof entry === "string" && entry.trim().length > 0)
      .map((entry) => entry.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_")),
  );
}

function capabilityObservationPresentation(summary: CapabilityObservationSummary | null | undefined): {
  available: boolean;
  label: string;
  detail: string;
  stateLabel: string;
  tone: AccessEvidenceTone;
} {
  const allowed = normalizedCapabilityNames(summary?.allowed);
  const denied = normalizedCapabilityNames(summary?.denied);
  const inconclusive = normalizedCapabilityNames(summary?.inconclusive);
  const available = summary?.evidence_available === true || (summary?.attempted || 0) > 0 || allowed.size > 0 || denied.size > 0 || inconclusive.size > 0;
  const writeCapabilities = new Set(["create_file", "create_directory", "modify_file", "delete"]);
  const dataWriteObserved = [...allowed].some((name) => writeCapabilities.has(name));
  const readable = allowed.has("read_file");
  const listable = allowed.has("list");
  const control = allowed.has("write_acl") || allowed.has("write_owner");
  const writable = dataWriteObserved || (summary?.writable_observed === true && !control);
  const connected = allowed.has("tree_connect");
  const stateLabel = summary?.partial === true
    ? "Capability checks partial"
    : summary?.complete === true
      ? "Capability checks complete"
      : available
        ? "Capability checks recorded"
        : "Capabilities not assessed";
  const method = summary?.method?.trim() ? ` Method: ${humanizeEvidenceValue(summary.method)}.` : "";

  if (!available) {
    return {
      available: false,
      label: "Capabilities not assessed",
      detail: "No collection-time capability observations were recorded.",
      stateLabel,
      tone: "neutral",
    };
  }

  let label = "Capability evidence recorded";
  let tone: AccessEvidenceTone = "neutral";
  if (readable && writable) {
    label = "Read/write observed";
    tone = "warning";
  } else if (readable) {
    label = "Read observed";
    tone = "positive";
  } else if (listable && writable) {
    label = "List/write observed";
    tone = "warning";
  } else if (writable) {
    label = "Write observed";
    tone = "warning";
  } else if (listable) {
    label = "List observed";
    tone = "warning";
  } else if (control) {
    label = "Control observed";
    tone = "warning";
  } else if (connected) {
    label = "Connection observed";
  } else if (allowed.size > 0) {
    label = "Capability observed";
  } else if (inconclusive.size > 0) {
    label = "Capability checks inconclusive";
    tone = "warning";
  } else if (denied.size > 0) {
    label = "Capability denials observed";
  }

  const outcomeFacts = [
    allowed.size > 0 ? `${allowed.size.toLocaleString()} allowed` : null,
    denied.size > 0 ? `${denied.size.toLocaleString()} denied` : null,
    inconclusive.size > 0 ? `${inconclusive.size.toLocaleString()} inconclusive` : null,
  ].filter(Boolean).join(", ");
  return {
    available: true,
    label,
    detail: `${outcomeFacts || "Collection-time operations were recorded"}. This is bounded operational evidence, not an effective-permissions calculation.${method}`,
    stateLabel,
    tone,
  };
}

export function presentAccessEvidence(summary: AccessEvidenceSummary | null | undefined): AccessEvidencePresentation {
  if (!summary) {
    return {
      label: "Evidence not available",
      detail: "This record predates the normalized access-evidence summary.",
      tone: "neutral",
      stateLabel: "Not assessed",
      coverageLabel: null,
      directPermissionsLabel: "Provider permission evidence not assessed",
      directPermissionsAssessed: false,
    };
  }

  const direct = summary.direct_permissions;
  const nestedDirectAvailable = direct?.evidence_available === true;
  const topLevelDirectAvailable = summary.direct_permissions_assessed === true
    || summary.direct_assessment_available === true
    || (summary.assessment_count || 0) > 0
    || (summary.entry_count || 0) > 0
    || (
      summary.evidence_available === true
      && summary.status !== "observed_capabilities"
      && summary.capability_observations?.evidence_available !== true
    );
  const directPermissionsAssessed = nestedDirectAvailable || topLevelDirectAvailable;
  const capability = capabilityObservationPresentation(summary.capability_observations);
  const evidenceAvailable = directPermissionsAssessed || capability.available || summary.evidence_available === true;
  const directState = (direct?.status || summary.assessment_state || summary.status || (summary.partial ? "partial" : "not_assessed")).toLowerCase();
  const outcome = (summary.outcome || (evidenceAvailable ? "observed" : "not_assessed")).toLowerCase();
  const permissionLabel = directPermissionsAssessed
    ? directState === "partial"
      ? "Partial permission evidence"
      : directState === "failed"
        ? "Permission assessment failed"
        : "Permission evidence available"
    : humanizeEvidenceValue(outcome);
  const label = summary.label?.trim() || (capability.available ? capability.label : permissionLabel);
  const stateLabel = capability.available
    ? capability.stateLabel
    : directPermissionsAssessed
      ? `Permission assessment ${humanizeEvidenceValue(directState).toLowerCase()}`
      : humanizeEvidenceValue(directState);
  const coverageLabel = typeof summary.coverage === "string"
    ? humanizeEvidenceValue(summary.coverage)
    : (direct?.comparable ?? summary.comparable) === true
      ? "Comparable Scope"
      : (direct?.comparable ?? summary.comparable) === false && directPermissionsAssessed
        ? "Non-comparable Scope"
        : null;
  const permissionCount = summary.direct_permission_count ?? direct?.entry_count ?? summary.entry_count;
  const directPermissionCountLabel = directPermissionsAssessed
    ? permissionCount == null
      ? "Provider permission evidence assessed"
      : `${permissionCount.toLocaleString()} provider permission ${permissionCount === 1 ? "entry" : "entries"}`
    : "Provider permission evidence not assessed";
  const directPermissionsLabel = directPermissionsAssessed && directState === "failed"
    ? permissionCount == null
      ? "Provider permission assessment failed"
      : `Provider permission assessment failed · ${permissionCount.toLocaleString()} ${permissionCount === 1 ? "entry" : "entries"} recorded`
    : directPermissionsAssessed && directState === "partial"
      ? `${directPermissionCountLabel} · partial assessment`
      : directPermissionCountLabel;

  return {
    label,
    detail:
      summary.detail?.trim() ||
      (capability.available
        ? capability.detail
        : `${stateLabel}${coverageLabel ? ` with ${coverageLabel.toLowerCase()} coverage` : ""}.`),
    tone: validTone(summary.tone) ? summary.tone : capability.available ? capability.tone : evidenceTone(outcome, directState),
    stateLabel,
    coverageLabel,
    directPermissionsLabel,
    directPermissionsAssessed,
  };
}

export function isDirectPermissionAssessment(assessment: AccessEvidenceAssessment): boolean {
  const values = [assessment.kind, assessment.semantics, assessment.permission_surface]
    .filter((value): value is string => typeof value === "string")
    .map((value) => value.toLowerCase());
  return values.some((value) =>
    value === "direct_permission" ||
    value === "direct_permissions" ||
    value === "acl_entry" ||
    value.includes("graph_permission") ||
    value.includes("windows_acl") ||
    value.endsWith("_dacl") ||
    value.endsWith("_permissions"),
  );
}

export function evidenceErrorText(error: string | { code?: string | null; message?: string | null }): string {
  if (typeof error === "string") return error;
  const message = error.message?.trim();
  const code = error.code?.trim();
  if (message && code) return `${message} (${code})`;
  return message || code || "Unknown collection error";
}

export function directPermissionEntryLabel(entry: DirectPermissionEntry): string {
  const principal = entry.principal;
  const providerLinkScope = typeof entry.provider_details?.link_scope === "string"
    ? entry.provider_details.link_scope
    : null;
  const providerPermissionKind = typeof entry.provider_details?.permission_kind === "string"
    ? entry.provider_details.permission_kind
    : null;
  return (
    principal?.display_name ||
    principal?.name ||
    principal?.email ||
    principal?.login_name ||
    entry.principal_display_name ||
    principal?.provider_principal_id ||
    principal?.native_id ||
    principal?.principal_key ||
    entry.principal_id ||
    (providerLinkScope ? `${humanizeEvidenceValue(providerLinkScope)} sharing link` : null) ||
    (providerPermissionKind ? humanizeEvidenceValue(providerPermissionKind) : null) ||
    "Unresolved principal"
  );
}

export function directPermissionEntryRights(entry: DirectPermissionEntry): string {
  const rights = [...(entry.roles || []), ...(entry.rights || []), ...(entry.normalized_rights || [])].filter(Boolean);
  const rightsLabel = rights.length > 0 ? [...new Set(rights)].join(", ") : "Rights not recorded";
  return entry.effect ? `${humanizeEvidenceValue(entry.effect)} · ${rightsLabel}` : rightsLabel;
}

export function directPermissionPrincipalDetail(entry: DirectPermissionEntry): string {
  const principal = entry.principal;
  if (!principal) return humanizeEvidenceValue(entry.entry_kind);
  const displayedLabel = directPermissionEntryLabel(entry);
  const nativeIdentifier = principal.provider_principal_id || principal.native_id || principal.principal_key;
  return [
    humanizeEvidenceValue(principal.principal_type || principal.type || principal.kind || entry.entry_kind),
    principal.resolution ? `Resolution: ${humanizeEvidenceValue(principal.resolution)}` : null,
    nativeIdentifier && nativeIdentifier !== displayedLabel ? `ID: ${nativeIdentifier}` : null,
    principal.authority ? `Authority: ${principal.authority}` : null,
  ].filter(Boolean).join(" · ");
}

function assessmentMergeKey(assessment: AccessEvidenceAssessment, index: number): string {
  if (assessment.id !== null && assessment.id !== undefined) return `id:${assessment.id}`;
  return [
    assessment.provider || "unknown",
    assessment.semantics || assessment.kind || "unknown",
    assessment.permission_surface || "unknown",
    assessment.subject?.key || assessment.subject?.provider_id || assessment.subject?.path || index,
  ].join(":");
}

function entryMergeKey(entry: DirectPermissionEntry, index: number): string {
  if (entry.id !== null && entry.id !== undefined) return `id:${entry.id}`;
  return entry.entry_key || entry.provider_entry_id || entry.source_permission_id || `${directPermissionEntryLabel(entry)}:${directPermissionEntryRights(entry)}:${index}`;
}

/** Merge cursor pages while preserving assessments and de-duplicating embedded entries. */
export function mergeAccessEvidenceDetails(current: AccessEvidenceDetail, incoming: AccessEvidenceDetail): AccessEvidenceDetail {
  const assessments = new Map<string, AccessEvidenceAssessment>();
  current.assessments.forEach((assessment, index) => assessments.set(assessmentMergeKey(assessment, index), assessment));
  incoming.assessments.forEach((assessment, index) => {
    const key = assessmentMergeKey(assessment, index);
    const existing = assessments.get(key);
    if (!existing) {
      assessments.set(key, assessment);
      return;
    }
    const entries = new Map<string, DirectPermissionEntry>();
    (existing.entries || []).forEach((entry, entryIndex) => entries.set(entryMergeKey(entry, entryIndex), entry));
    (assessment.entries || []).forEach((entry, entryIndex) => entries.set(entryMergeKey(entry, entryIndex), entry));
    assessments.set(key, { ...existing, ...assessment, entries: [...entries.values()] });
  });
  return {
    resource: incoming.resource || current.resource,
    overall: { ...current.overall, ...incoming.overall },
    assessments: [...assessments.values()],
    provenance: {
      ...(current.provenance || {}),
      ...(incoming.provenance || {}),
      pagination: incoming.provenance?.pagination || current.provenance?.pagination || null,
    },
  };
}
