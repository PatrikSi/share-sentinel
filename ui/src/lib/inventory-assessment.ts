import type { CollectionContext, ProviderMetadata } from "@/lib/provider-context";

export type AssessmentTone = "positive" | "warning" | "negative" | "neutral";

export type AssessmentStatus = {
  key: string;
  label: string;
  tone: AssessmentTone;
  detail?: string;
};

export type AssessmentDetail = {
  label: string;
  value: string;
};

export type SharePointAssessment = {
  availability: AssessmentStatus;
  lifecycle: AssessmentStatus;
  content: AssessmentStatus | null;
  details: AssessmentDetail[];
  evidence: string | null;
  fileCount: number | null;
  folderCount: number | null;
  itemCount: number | null;
  totalSizeBytes: number | null;
  canViewItems: boolean;
};

type SharePointAssessmentInput = {
  scope: "endpoint" | "resource";
  metadata?: ProviderMetadata | null;
  endpointMetadata?: ProviderMetadata | null;
  itemCount?: number | null;
  collectionContext?: CollectionContext | null;
};

function normalizedToken(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return value.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
}

function metadataToken(metadata: ProviderMetadata | null | undefined, ...keys: string[]): string | null {
  if (!metadata) return null;
  for (const key of keys) {
    const normalized = normalizedToken(metadata[key]);
    if (normalized) return normalized;
  }
  return null;
}

function metadataText(metadata: ProviderMetadata | null | undefined, ...keys: string[]): string | null {
  if (!metadata) return null;
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function metadataBoolean(metadata: ProviderMetadata | null | undefined, ...keys: string[]): boolean | null {
  if (!metadata) return null;
  for (const key of keys) {
    if (typeof metadata[key] === "boolean") return metadata[key] as boolean;
  }
  return null;
}

function metadataCount(metadata: ProviderMetadata | null | undefined, ...keys: string[]): number | null {
  if (!metadata) return null;
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  }
  return null;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function formatAssessmentBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not recorded";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  if (unitIndex === 0) return `${value.toLocaleString()} B`;
  return `${amount.toFixed(amount < 10 ? 1 : 0)} ${units[unitIndex]}`;
}

function availabilityStatus(
  metadata: ProviderMetadata | null | undefined,
  endpointMetadata: ProviderMetadata | null | undefined,
): AssessmentStatus {
  const existence = metadataToken(metadata, "existence_status", "availability_status", "existence")
    || metadataToken(endpointMetadata, "existence_status", "availability_status", "existence");
  const assessment = metadataToken(metadata, "assessment") || metadataToken(endpointMetadata, "assessment");
  const lifecycle = metadataToken(metadata, "lifecycle_state") || metadataToken(endpointMetadata, "lifecycle_state");
  const values = [existence, assessment, lifecycle].filter((value): value is string => value !== null);

  if (values.some((value) => ["confirmed", "confirmed_from_discovery", "exists", "resolved", "available"].includes(value))) {
    return { key: "confirmed", label: "Exists", tone: "positive", detail: "Microsoft Graph resolved this resource when it was collected." };
  }
  if (values.some((value) => ["not_found", "not_found_or_not_visible", "missing"].includes(value))) {
    return { key: "not_found", label: "Not found or not visible", tone: "negative", detail: "Microsoft Graph returned not found. The target may be missing, or it may be hidden from this assessment identity." };
  }
  if (values.some((value) => ["inaccessible", "permission_denied", "forbidden"].includes(value))) {
    return { key: "inaccessible", label: "Inaccessible", tone: "warning", detail: "The assessment identity could not verify the target. This does not prove it is missing." };
  }
  if (values.includes("authentication_failed")) {
    return { key: "authentication_failed", label: "Authentication failed", tone: "negative", detail: "The token could not authenticate to Microsoft Graph, so existence was not verified." };
  }
  if (values.some((value) => ["temporarily_unreachable", "transient_failure"].includes(value))) {
    return { key: "temporarily_unreachable", label: "Temporarily unreachable", tone: "warning", detail: "A transient provider or network failure prevented verification." };
  }
  if (values.includes("invalid_target")) {
    return { key: "invalid_target", label: "Invalid target", tone: "negative", detail: "The supplied SharePoint target could not be parsed or resolved safely." };
  }
  return { key: "unknown", label: "Existence unknown", tone: "neutral", detail: "No conclusive existence result was recorded." };
}

function lifecycleStatus(
  metadata: ProviderMetadata | null | undefined,
  endpointMetadata: ProviderMetadata | null | undefined,
): AssessmentStatus {
  const lifecycle = metadataToken(metadata, "lifecycle_state", "lifecycle_status")
    || metadataToken(endpointMetadata, "lifecycle_state", "lifecycle_status");
  const archive = metadataToken(metadata, "archive_status") || metadataToken(endpointMetadata, "archive_status");
  const value = lifecycle || archive;

  if (archive === "fully_archived") {
    return { key: "archived", label: "Fully archived", tone: "warning", detail: "Graph reported the site as fully archived." };
  }
  if (archive === "recently_archived") {
    return { key: "archived", label: "Recently archived", tone: "warning", detail: "Graph reported the site as recently archived." };
  }
  if (value === "archived") {
    return { key: "archived", label: "Archived", tone: "warning", detail: "Graph reported an archived lifecycle state." };
  }
  if (value === "reactivating") {
    return { key: "reactivating", label: "Reactivating", tone: "warning", detail: "Graph reported that the site is being reactivated." };
  }
  if (archive === "not_archived") {
    return { key: "not_archived", label: "Not archived", tone: "positive", detail: "Graph returned authoritative archive evidence and no archival state." };
  }
  if (value === "available" || value === "active") {
    return { key: "available", label: "Available", tone: "positive", detail: "Graph reported an available lifecycle state." };
  }
  if (value === "not_found") {
    return { key: "not_found", label: "Lifecycle unavailable", tone: "neutral", detail: "A missing target has no observable lifecycle state." };
  }
  if (value === "inaccessible") {
    return { key: "inaccessible", label: "Lifecycle unverified", tone: "neutral", detail: "Permissions prevented lifecycle inspection." };
  }
  return { key: "unknown", label: "Lifecycle unknown", tone: "neutral", detail: "No authoritative archive state was recorded." };
}

function contentStatus(
  metadata: ProviderMetadata | null | undefined,
  collectionContext: CollectionContext | null | undefined,
): AssessmentStatus {
  const enumeration = metadataToken(metadata, "enumeration_status", "inventory_status");
  const content = metadataToken(metadata, "content_state", "content_status");
  const complete = metadataBoolean(metadata, "collection_complete", "enumeration_complete");
  const filesIncluded = metadataBoolean(collectionContext?.metadata, "files_included", "filesIncluded");

  // Empty is intentionally strict: count=0 on its own can also mean disabled,
  // denied, failed, truncated, or legacy collection.
  if (enumeration === "complete" && content === "empty") {
    return { key: "empty", label: "Empty", tone: "neutral", detail: "Complete metadata enumeration returned no files or folders." };
  }
  if (enumeration === "complete" && content === "populated") {
    return { key: "populated", label: "Populated", tone: "positive", detail: "Complete metadata enumeration returned files or folders." };
  }
  if (enumeration === "complete" || complete === true) {
    return { key: "complete", label: "Enumerated", tone: "positive", detail: "Metadata enumeration completed, but no explicit empty/populated classification was recorded." };
  }
  if (enumeration === "in_progress") {
    return { key: "in_progress", label: "Enumerating", tone: "warning", detail: "File and folder enumeration had not finished when this record was emitted." };
  }
  if (enumeration === "not_requested" || content === "not_assessed" || filesIncluded === false) {
    return { key: "not_requested", label: "Not enumerated", tone: "neutral", detail: "This assessment did not request file and folder enumeration." };
  }
  if (enumeration === "permission_denied") {
    return { key: "permission_denied", label: "Enumeration denied", tone: "warning", detail: "The assessment identity could see the library but could not enumerate its contents." };
  }
  if (enumeration === "authentication_failed") {
    return { key: "authentication_failed", label: "Authentication failed", tone: "negative", detail: "The collector could not authenticate while enumerating this library." };
  }
  if (enumeration === "not_found" || enumeration === "not_found_or_not_visible") {
    return { key: "not_found", label: "Library not found or not visible", tone: "negative", detail: "Graph returned not found during enumeration. The library may be missing, or it may be hidden from this assessment identity." };
  }
  if (enumeration === "failed") {
    return { key: "failed", label: "Enumeration failed", tone: "negative", detail: "File and folder enumeration failed. A zero row count is not evidence that the library is empty." };
  }
  if (enumeration === "temporarily_unreachable") {
    return { key: "temporarily_unreachable", label: "Temporarily unreachable", tone: "warning", detail: "A transient provider or network failure interrupted enumeration. Retry before drawing a content conclusion." };
  }
  if (enumeration === "indeterminate" || complete === false) {
    return { key: "indeterminate", label: "Enumeration incomplete", tone: "warning", detail: "Collection did not produce complete content evidence." };
  }
  return { key: "unknown", label: "Content unknown", tone: "neutral", detail: "This record predates explicit enumeration status or did not include it." };
}

function evidenceSummary(metadata: ProviderMetadata | null | undefined, endpointMetadata: ProviderMetadata | null | undefined): string | null {
  const raw = metadata?.evidence ?? endpointMetadata?.evidence;
  if (typeof raw === "string" && raw.trim()) return raw.trim();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const evidence = raw as ProviderMetadata;
  const parts: string[] = [];
  const basis = metadataText(evidence, "basis");
  const statusCode = evidence.graph_status_code;
  const errorCode = metadataText(evidence, "graph_error_code");
  const authoritative = metadataBoolean(evidence, "archive_status_authoritative");
  const archiveChecked = metadataBoolean(evidence, "archive_status_checked");
  const archiveSource = metadataText(evidence, "archive_status_source");
  const archiveScope = metadataText(evidence, "archive_status_scope");
  const archiveSiteCollectionId = metadataText(evidence, "archive_status_site_collection_id");
  if (basis) parts.push(`Basis: ${humanize(basis)}`);
  if ((typeof statusCode === "number" && Number.isInteger(statusCode)) || (typeof statusCode === "string" && statusCode.trim())) {
    parts.push(`Graph status: ${String(statusCode)}`);
  }
  if (errorCode) parts.push(`Graph error: ${errorCode}`);
  if (archiveChecked !== null) parts.push(`Archive status checked: ${archiveChecked ? "yes" : "no"}`);
  if (authoritative !== null) parts.push(`Archive status authoritative: ${authoritative ? "yes" : "no"}`);
  if (archiveSource) parts.push(`Archive source: ${archiveSource}`);
  if (archiveScope) parts.push(`Archive scope: ${humanize(archiveScope)}`);
  if (archiveSiteCollectionId) parts.push(`Site collection: ${archiveSiteCollectionId}`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

export function deriveSharePointAssessment({
  scope,
  metadata,
  endpointMetadata,
  itemCount: rowItemCount,
  collectionContext,
}: SharePointAssessmentInput): SharePointAssessment {
  const availability = availabilityStatus(metadata, endpointMetadata);
  const lifecycle = lifecycleStatus(metadata, endpointMetadata);
  const content = scope === "resource" ? contentStatus(metadata, collectionContext) : null;
  const fileCount = metadataCount(metadata, "file_count", "files");
  const folderCount = metadataCount(metadata, "folder_count", "folders");
  const recordedItemCount = metadataCount(metadata, "item_count", "items");
  const itemCount = recordedItemCount ?? (typeof rowItemCount === "number" && rowItemCount >= 0 ? rowItemCount : null);
  const totalSizeBytes = metadataCount(metadata, "total_size_bytes", "size_bytes");
  const enumerationStatus = metadataToken(metadata, "enumeration_status", "inventory_status");
  const syncMode = metadataText(metadata, "sync_mode");
  const errorCode = metadataText(metadata, "enumeration_error_code", "assessment_error_code");
  const requestedTarget = metadataText(metadata, "requested_target") || metadataText(endpointMetadata, "requested_target");
  const checkedAt = metadataText(metadata, "assessment_checked_at", "checked_at")
    || metadataText(endpointMetadata, "assessment_checked_at", "checked_at");
  const sizeComplete = metadataBoolean(metadata, "size_observation_complete");
  const details: AssessmentDetail[] = [
    { label: "Existence", value: availability.label },
    { label: "Lifecycle", value: lifecycle.label },
  ];

  if (content) details.push({ label: "Content", value: content.label });
  if (enumerationStatus) details.push({ label: "Enumeration", value: humanize(enumerationStatus) });
  if (fileCount !== null) details.push({ label: "Files", value: fileCount.toLocaleString() });
  if (folderCount !== null) details.push({ label: "Folders", value: folderCount.toLocaleString() });
  if (itemCount !== null) details.push({ label: recordedItemCount === null ? "Collected rows" : "Items", value: itemCount.toLocaleString() });
  if (totalSizeBytes !== null) {
    details.push({
      label: sizeComplete === false ? "Observed size (partial)" : "Total size",
      value: formatAssessmentBytes(totalSizeBytes),
    });
  }
  if (syncMode) details.push({ label: "Sync mode", value: humanize(syncMode) });
  if (errorCode) details.push({ label: "Enumeration error", value: errorCode });
  if (requestedTarget) details.push({ label: "Requested target", value: requestedTarget });
  if (checkedAt) details.push({ label: "Assessed", value: checkedAt });

  return {
    availability,
    lifecycle,
    content,
    details,
    evidence: evidenceSummary(metadata, endpointMetadata),
    fileCount,
    folderCount,
    itemCount,
    totalSizeBytes,
    canViewItems: scope === "resource" && (fileCount !== null ? fileCount > 0 : itemCount !== null && itemCount > 0),
  };
}
