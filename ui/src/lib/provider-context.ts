export type ProviderMetadata = Record<string, unknown>;

export type CollectionContext = {
  source?: string | null;
  provider?: string | null;
  collection_mode?: string | null;
  auth_mode?: string | null;
  auth_type?: string | null;
  tenant_id?: string | null;
  tenant_name?: string | null;
  user_id?: string | null;
  user_principal_name?: string | null;
  client_id?: string | null;
  scopes?: string[] | null;
  roles?: string[] | null;
  token_expiration?: string | null;
  assessed_identity?: string | null;
  status?: string | null;
  partial?: boolean | null;
  discovery_completeness?: string | null;
  sync_mode?: string | null;
  materialized_snapshot?: boolean | null;
  metadata?: ProviderMetadata | null;
};

export type Exposure =
  | "USER_VISIBLE"
  | "BROAD_INTERNAL"
  | "EXTERNAL"
  | "ANONYMOUS"
  | "RESTRICTED"
  | "UNKNOWN";

export const EXPOSURE_VALUES: Exposure[] = [
  "ANONYMOUS",
  "EXTERNAL",
  "BROAD_INTERNAL",
  "USER_VISIBLE",
  "RESTRICTED",
  "UNKNOWN",
];

const EXPOSURE_SET = new Set<string>(EXPOSURE_VALUES);

export function normalizedProvider(value: unknown, ...fallbacks: unknown[]): string {
  for (const candidate of [value, ...fallbacks]) {
    if (typeof candidate !== "string") continue;
    const normalized = candidate.trim().toLowerCase();
    if (!normalized) continue;
    if (normalized === "sharepoint_library" || normalized === "sharepoint_online") return "sharepoint";
    if (normalized === "smb_share") return "smb";
    if (normalized === "nfs_share") return "nfs";
    return normalized;
  }
  return "unknown";
}

export function providerLabel(provider: unknown, ...fallbacks: unknown[]): string {
  const normalized = normalizedProvider(provider, ...fallbacks);
  if (normalized === "sharepoint") return "SharePoint";
  if (normalized === "smb") return "SMB";
  if (normalized === "nfs") return "NFS";
  if (normalized === "unknown") return "Unknown source";
  return normalized.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function resourceTypeLabel(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "Unknown resource";
  const normalized = value.trim().toLowerCase();
  if (normalized === "sharepoint_library") return "Document library";
  if (normalized === "smb_share") return "SMB share";
  if (normalized === "nfs_share") return "NFS export";
  return normalized.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function normalizeExposure(value: unknown): Exposure {
  if (typeof value !== "string") return "UNKNOWN";
  const normalized = value.trim().toUpperCase();
  return EXPOSURE_SET.has(normalized) ? (normalized as Exposure) : "UNKNOWN";
}

export function exposureLabel(value: unknown): string {
  switch (normalizeExposure(value)) {
    case "USER_VISIBLE":
      return "User-visible";
    case "BROAD_INTERNAL":
      return "Broad internal";
    case "EXTERNAL":
      return "External access";
    case "ANONYMOUS":
      return "Anonymous access";
    case "RESTRICTED":
      return "Restricted";
    default:
      return "Unknown";
  }
}

export function exposureDescription(value: unknown): string {
  switch (normalizeExposure(value)) {
    case "USER_VISIBLE":
      return "The assessed delegated identity can discover or read this resource. This does not mean it is public or broadly shared.";
    case "BROAD_INTERNAL":
      return "Evidence indicates access by a broad organization-wide internal principal or group.";
    case "EXTERNAL":
      return "Evidence indicates that guest or external identities may have access.";
    case "ANONYMOUS":
      return "Evidence indicates an unauthenticated Anyone link or equivalent anonymous access path.";
    case "RESTRICTED":
      return "No broad, external, or anonymous exposure was identified with the available assessment evidence.";
    default:
      return "The collector did not have enough evidence or permission to classify exposure.";
  }
}

export function exposureEvidenceSummary(evidence: ProviderMetadata | null | undefined): string | null {
  if (!evidence || Object.keys(evidence).length === 0) return null;
  const details: string[] = [];
  const basis = metadataString(evidence, "basis", "classification_basis", "classificationBasis");
  const identity = metadataString(evidence, "assessed_identity", "assessedIdentity");
  const scope = metadataString(evidence, "classification_scope", "classificationScope", "link_scope", "linkScope", "scope");
  if (basis === "graph_delegated_read_context") details.push("Observed through delegated Graph read context");
  else if (basis === "exposure_not_assessed") details.push("Exposure permission evidence was not assessed");
  else if (basis) details.push(`Basis: ${basis.replaceAll("_", " ")}`);
  if (identity) details.push(`Identity: ${identity}`);
  if (scope === "visibility_not_public_exposure") details.push("Visibility only; not evidence of public access");
  else if (scope) details.push(`Scope: ${scope.replaceAll("_", " ")}`);
  return details.length > 0 ? details.join(". ") : "Evidence metadata is recorded for this classification.";
}

export function metadataString(metadata: ProviderMetadata | null | undefined, ...keys: string[]): string | null {
  if (!metadata) return null;
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

export function metadataBoolean(metadata: ProviderMetadata | null | undefined, ...keys: string[]): boolean | null {
  if (!metadata) return null;
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "boolean") return value;
  }
  return null;
}

export function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate || candidate.length > 2_048) return null;
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function collectionContextProvider(context: CollectionContext | null | undefined): string {
  return normalizedProvider(context?.provider, context?.source);
}

export function isSharePointContext(context: CollectionContext | null | undefined): boolean {
  return collectionContextProvider(context) === "sharepoint";
}

export function collectionModeLabel(value: unknown): string {
  if (value === "delegated_user_view") return "Delegated user view";
  if (value === "tenant_inventory") return "Application tenant inventory";
  if (typeof value !== "string" || !value.trim()) return "Collection scope unknown";
  return value.trim().replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function authModeLabel(authMode: unknown, authType: unknown): string {
  const mode = typeof authMode === "string" && authMode.trim() ? authMode.trim().replaceAll("_", " ") : "unknown mode";
  const type = typeof authType === "string" && authType.trim() ? authType.trim().replaceAll("_", " ") : "unknown identity type";
  return `${mode.replace(/\b\w/g, (character) => character.toUpperCase())} · ${type.replace(/\b\w/g, (character) => character.toUpperCase())}`;
}

export function assessedIdentity(context: CollectionContext | null | undefined): string | null {
  return context?.assessed_identity || context?.user_principal_name || context?.user_id || null;
}

export function collectionCoverageText(context: CollectionContext | null | undefined): string {
  if (!context) return "Collection context was not recorded for this run.";
  const provider = collectionContextProvider(context);
  if (provider !== "sharepoint") {
    return "Scope and access semantics are determined by the collector context recorded with this run.";
  }
  if (context.collection_mode === "delegated_user_view") {
    const identity = assessedIdentity(context);
    return identity
      ? `Security-trimmed resources visible to ${identity}; these results are not an authoritative tenant inventory.`
      : "Security-trimmed resources visible to the delegated identity; these results are not an authoritative tenant inventory.";
  }
  if (context.collection_mode === "tenant_inventory") {
    return "Tenant inventory from the application's granted Microsoft Graph permissions; excluded or inaccessible sites may not be represented.";
  }
  return "SharePoint collection perspective was not recorded. Treat coverage as unknown.";
}

export function collectionIsPartial(context: CollectionContext | null | undefined): boolean {
  if (!context) return false;
  if (context.partial === true) return true;
  const completeness = context.discovery_completeness?.trim().toLowerCase();
  if (completeness && ["partial", "incomplete", "truncated", "failed"].includes(completeness)) return true;
  return typeof context.status === "string" && ["partial", "failed", "error"].includes(context.status.trim().toLowerCase());
}

export function collectionLimitationLabel(context: CollectionContext | null | undefined): string | null {
  if (!context) return null;
  const status = context.status?.trim().toLowerCase();
  if (status === "failed" || status === "error") return "Collection failed";
  if (context.partial === true || status === "partial") return "Partial results";
  const completeness = context.discovery_completeness?.trim().toLowerCase();
  if (!completeness) {
    return metadataBoolean(context.metadata, "discovery_complete", "discoveryComplete") === false
      ? "Non-authoritative coverage"
      : null;
  }
  if (["complete", "authoritative", "complete_for_granted_scope"].includes(completeness)) return null;
  if (["partial", "incomplete", "truncated", "failed"].includes(completeness)) return "Partial or incomplete discovery";
  if (completeness === "security_trimmed_or_partial") return "Security-trimmed / non-authoritative coverage";
  if (completeness === "security_trimmed") return "Security-trimmed coverage";
  if (["targeted", "targeted_scope"].includes(completeness)) return "Targeted scope";
  if (completeness === "non_authoritative") return "Non-authoritative coverage";
  if (["unknown", "not_reported"].includes(completeness)) return "Completeness unknown";
  return `${completeness.replaceAll("_", " ")} coverage`;
}

export function collectionSnapshotLabel(context: CollectionContext | null | undefined): string | null {
  const value = context?.sync_mode || metadataString(
    context?.metadata,
    "sync_mode",
    "syncMode",
    "snapshot_mode",
    "snapshotMode",
    "collection_phase",
    "collectionPhase",
  );
  const materialized = context?.materialized_snapshot ?? metadataBoolean(context?.metadata, "snapshot_materialized", "snapshotMaterialized");
  if (!value) {
    if (materialized === true) return "Materialized snapshot";
    if (materialized === false) return "Non-materialized collection";
    return null;
  }
  const normalized = value.toLowerCase();
  if (normalized === "initial" || normalized === "full") return materialized ? "Full materialized snapshot" : "Initial full collection";
  if (normalized === "incremental" || normalized === "delta") return "Incremental materialized snapshot";
  if (normalized === "mixed") return materialized ? "Mixed full/delta materialized snapshot" : "Mixed full/delta collection";
  if (normalized === "metadata_only") return "Metadata-only snapshot";
  if (normalized === "none") return "No item snapshot";
  if (normalized === "materialized_snapshot" || normalized === "materialized") return "Materialized snapshot";
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function compareCollectionContexts(
  current: CollectionContext | null | undefined,
  baseline: CollectionContext | null | undefined,
): { known: boolean; compatible: boolean; reasons: string[] } {
  const currentRecorded = !!current && Object.keys(current).length > 0;
  const baselineRecorded = !!baseline && Object.keys(baseline).length > 0;
  if (!currentRecorded || !baselineRecorded) {
    return {
      known: false,
      compatible: false,
      reasons: [!currentRecorded && !baselineRecorded ? "Neither run records collection context." : "One run does not record collection context."],
    };
  }

  const reasons: string[] = [];
  const currentProvider = collectionContextProvider(current);
  const baselineProvider = collectionContextProvider(baseline);
  if (currentProvider !== baselineProvider) reasons.push(`Source changed from ${providerLabel(baselineProvider)} to ${providerLabel(currentProvider)}.`);
  if ((current?.source || "") !== (baseline?.source || "")) reasons.push("Recorded collection source changed.");
  if ((current?.collection_mode || "") !== (baseline?.collection_mode || "")) reasons.push("Collection perspective changed (for example, application inventory versus delegated user view).");
  if ((current?.auth_mode || "") !== (baseline?.auth_mode || "")) reasons.push("Authentication mode changed.");
  if ((current?.auth_type || "") !== (baseline?.auth_type || "")) reasons.push("Authentication identity type changed.");
  if ((current?.tenant_id || "") !== (baseline?.tenant_id || "")) reasons.push("Tenant identity changed.");
  if ((current?.client_id || "") !== (baseline?.client_id || "")) reasons.push("Application client identity changed.");
  if (
    (current?.collection_mode === "delegated_user_view" || baseline?.collection_mode === "delegated_user_view") &&
    assessedIdentity(current) !== assessedIdentity(baseline)
  ) {
    reasons.push("The delegated assessed identity changed.");
  }
  const currentScopes = [...(current?.scopes || [])].map(String).sort();
  const baselineScopes = [...(baseline?.scopes || [])].map(String).sort();
  if (JSON.stringify(currentScopes) !== JSON.stringify(baselineScopes)) reasons.push("Delegated Graph scopes changed.");
  const currentRoles = [...(current?.roles || [])].map(String).sort();
  const baselineRoles = [...(baseline?.roles || [])].map(String).sort();
  if (JSON.stringify(currentRoles) !== JSON.stringify(baselineRoles)) reasons.push("Application Graph roles changed.");
  const currentMaterialized = current?.materialized_snapshot ?? metadataBoolean(current?.metadata, "snapshot_materialized", "snapshotMaterialized");
  const baselineMaterialized = baseline?.materialized_snapshot ?? metadataBoolean(baseline?.metadata, "snapshot_materialized", "snapshotMaterialized");
  if (currentMaterialized !== baselineMaterialized) reasons.push("Snapshot materialization semantics changed.");
  if (currentMaterialized === false || baselineMaterialized === false) reasons.push("At least one run is not a materialized snapshot.");
  if ((current?.discovery_completeness || "") !== (baseline?.discovery_completeness || "")) reasons.push("Discovery completeness changed.");
  for (const [key, label] of [
    ["discovery_strategy", "Discovery strategy changed."],
    ["discovery_authoritative", "Discovery authority claim changed."],
    ["permissions_assessed", "Permission assessment coverage changed."],
  ] as const) {
    if (current?.metadata?.[key] !== baseline?.metadata?.[key]) reasons.push(label);
  }
  const currentTargetScope = current?.metadata?.collection && typeof current.metadata.collection === "object"
    ? (current.metadata.collection as ProviderMetadata).target_scope
    : undefined;
  const baselineTargetScope = baseline?.metadata?.collection && typeof baseline.metadata.collection === "object"
    ? (baseline.metadata.collection as ProviderMetadata).target_scope
    : undefined;
  if (JSON.stringify(currentTargetScope) !== JSON.stringify(baselineTargetScope)) reasons.push("Target scope changed.");
  if (collectionIsPartial(current) || collectionIsPartial(baseline)) reasons.push("At least one run has partial, incomplete, or failed discovery coverage.");
  return { known: true, compatible: reasons.length === 0, reasons };
}
