export type CapabilityStatus = "allowed" | "denied" | "mixed" | "not_tested" | "inconclusive";

export type AccessCapabilityEvidence = {
  status: CapabilityStatus;
  [key: string]: unknown;
};

export type AccessCapabilityMetadata = Record<string, unknown>;

export type AccessCapabilities = Record<string, AccessCapabilityEvidence | AccessCapabilityMetadata | undefined> & {
  _metadata?: AccessCapabilityMetadata;
};

type AccessCapabilityCellProps = {
  accessLevel: string;
  capabilities: AccessCapabilities | null | undefined;
  evidenceScope?: string;
  label: string;
  onCopy?: (value: string, label: string) => void;
  onFilter?: (value: string, negated: boolean) => void;
};

type AccessSummary = {
  icon: string;
  label: string;
  tone: "positive" | "warning" | "negative" | "neutral";
  detail?: string;
};

const CAPABILITY_ORDER = [
  "tree_connect",
  "list",
  "read_file",
  "create_file",
  "create_directory",
  "modify_file",
  "delete",
  "write_acl",
  "write_owner",
] as const;

const CAPABILITY_LABELS: Record<string, string> = {
  tree_connect: "Connect to share",
  list: "List folder",
  read_file: "Read file",
  create_file: "Create file",
  create_directory: "Create folder",
  modify_file: "Modify file",
  delete: "Delete",
  write_acl: "Change ACL",
  write_owner: "Change owner",
};

const COMPACT_WRITE_CAPABILITIES = [
  "create_file",
  "create_directory",
  "modify_file",
  "delete",
  "write_acl",
  "write_owner",
] as const;

const STATUS_PRESENTATION: Record<CapabilityStatus, { icon: string; label: string }> = {
  allowed: { icon: "✓", label: "Allowed" },
  denied: { icon: "×", label: "Denied" },
  mixed: { icon: "±", label: "Mixed" },
  not_tested: { icon: "—", label: "Not tested" },
  inconclusive: { icon: "?", label: "Inconclusive" },
};

const SMB_ASSESSMENT_SUMMARIES: Record<string, AccessSummary> = {
  read_write_observed: { icon: "RW", label: "Read/write observed", tone: "warning" },
  read_observed: { icon: "R", label: "Read observed", tone: "positive" },
  list_write_observed: { icon: "LW", label: "List/write observed", tone: "warning" },
  list_observed: { icon: "L", label: "List observed", tone: "warning" },
  write_observed: { icon: "W", label: "Write observed", tone: "warning" },
  control_observed: { icon: "C", label: "Control observed", tone: "warning" },
  connected_list_denied: { icon: "C", label: "Connected; list denied", tone: "warning" },
  connected_only: { icon: "C", label: "Connection observed", tone: "neutral" },
  tree_denied: { icon: "×", label: "Share connection denied", tone: "negative" },
  inconclusive: { icon: "?", label: "Assessment inconclusive", tone: "warning" },
  not_assessed: { icon: "—", label: "Not assessed", tone: "neutral" },
};

const SMB_ASSESSMENT_REASON_LABELS: Record<string, string> = {
  pending: "Assessment had not finished",
  listing_truncated: "Listing reached its configured limit",
  partial_transport_failure: "Transport failed after partial evidence was collected",
  cancelled_after_observation: "Assessment was cancelled after partial evidence was collected",
  no_visible_file_candidate: "No visible file was available for file-level probes",
  probes_disabled: "Capability probes were disabled",
  share_unavailable: "The share could not be reached",
  object_not_found: "The sampled object was no longer available",
  sharing_violation: "The sampled object was locked or shared incompatibly",
  object_state_changed: "The sampled object changed during assessment",
  transport_failure: "The SMB transport failed before a conclusive result",
  tree_session_invalid: "The server invalidated the SMB share tree, so remaining probes were stopped",
  probe_aborted: "The remaining non-mutating probes were stopped after the share session became unusable",
  protocol_error: "The server returned an inconclusive SMB protocol error",
  unsupported_request: "The server did not support the requested non-mutating probe",
  invalid_request: "The server rejected the non-mutating probe request",
  legacy_operation_refused: "The SMB1 server refused the operation without a precise status",
  capacity_constraint: "The server could not evaluate the probe because of a capacity constraint",
  storage_unavailable: "Backing storage was unavailable during the probe",
  object_type_mismatch: "The sampled path changed type or did not match the requested probe",
  collector_error: "The collector isolated an unexpected response to this share",
  transport_aborted: "The remaining probes were stopped after a transport failure",
  tree_unavailable: "A share tree connection was not available for this probe",
  probe_method_unavailable: "The server did not expose the required non-mutating probe method",
  not_reached: "Collection ended before this capability could be tested",
  no_conclusive_evidence: "No conclusive list, read, or write evidence was collected",
};

function normalizeCapabilityStatus(value: unknown): CapabilityStatus {
  if (value === "allowed" || value === "denied" || value === "mixed" || value === "not_tested" || value === "inconclusive") {
    return value;
  }
  return "inconclusive";
}

function isObserved(evidence: AccessCapabilityEvidence | undefined): boolean {
  const status = normalizeCapabilityStatus(evidence?.status);
  return status === "allowed" || status === "mixed";
}

function capabilityMetadataString(capabilities: AccessCapabilities | null | undefined, key: string): string | null {
  const value = capabilities?._metadata?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function capabilityMetadataBoolean(capabilities: AccessCapabilities | null | undefined, key: string): boolean | null {
  const value = capabilities?._metadata?.[key];
  return typeof value === "boolean" ? value : null;
}

function normalizedMetadataToken(value: string | null): string | null {
  return value?.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_") || null;
}

function assessmentReasonDetail(capabilities: AccessCapabilities | null | undefined): string | undefined {
  const reason = normalizedMetadataToken(capabilityMetadataString(capabilities, "assessment_reason"));
  if (!reason || reason === "bounded_observation") return undefined;
  return SMB_ASSESSMENT_REASON_LABELS[reason] || humanizeIdentifier(reason);
}

function capabilityEvidence(
  capabilities: AccessCapabilities | null | undefined,
  key: string,
): AccessCapabilityEvidence | undefined {
  const value = capabilities?.[key];
  if (!value || typeof value !== "object" || Array.isArray(value) || !("status" in value)) return undefined;
  return value as AccessCapabilityEvidence;
}

function legacyAccessSummary(accessLevel: string): AccessSummary {
  const normalized = accessLevel.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  if (["readable", "read", "read_only", "read_write"].includes(normalized)) {
    return { icon: "R", label: "Read observed", tone: "positive" };
  }
  if (["list_only", "list", "browse"].includes(normalized)) {
    return { icon: "L", label: "List observed", tone: "warning" };
  }
  if (["no_access", "denied", "not_listable"].includes(normalized)) {
    return { icon: "×", label: "Access denied", tone: "negative" };
  }
  return { icon: "?", label: "Unknown", tone: "neutral" };
}

function accessSummary(accessLevel: string, capabilities: AccessCapabilities | null | undefined): AccessSummary {
  const entries = capabilityEntries(capabilities);
  const readObserved = isObserved(capabilityEvidence(capabilities, "read_file"));
  const listObserved = isObserved(capabilityEvidence(capabilities, "list"));
  const writeObserved = ["create_file", "create_directory", "modify_file", "delete"].some((key) =>
    isObserved(capabilityEvidence(capabilities, key)),
  );
  const controlObserved = ["write_acl", "write_owner"].some((key) => isObserved(capabilityEvidence(capabilities, key)));
  const anyObserved = entries.some(([key, evidence]) => key !== "tree_connect" && isObserved(evidence));

  if (readObserved && writeObserved) {
    return { icon: "RW", label: "Read/write observed", tone: "warning" };
  }
  if (readObserved) {
    return { icon: "R", label: "Read observed", tone: "positive" };
  }
  if (listObserved && writeObserved) {
    return { icon: "LW", label: "List/write observed", tone: "warning" };
  }
  if (writeObserved) {
    return { icon: "W", label: "Write observed", tone: "warning" };
  }
  if (listObserved) {
    return { icon: "L", label: "List observed", tone: "warning" };
  }
  if (controlObserved) {
    return { icon: "C", label: "Control observed", tone: "warning" };
  }
  if (anyObserved) {
    return { icon: "+", label: "Access observed", tone: "neutral" };
  }

  const explicitSummary = normalizedMetadataToken(capabilityMetadataString(capabilities, "assessment_summary"));
  const explicitPresentation = explicitSummary ? SMB_ASSESSMENT_SUMMARIES[explicitSummary] : null;
  if (explicitPresentation) {
    return { ...explicitPresentation, detail: assessmentReasonDetail(capabilities) };
  }

  const connectionStatus = normalizeCapabilityStatus(capabilityEvidence(capabilities, "tree_connect")?.status);
  const listStatus = normalizeCapabilityStatus(capabilityEvidence(capabilities, "list")?.status);
  const connectionDenied = connectionStatus === "denied";
  if (connectionDenied) {
    return { icon: "×", label: "Share connection denied", tone: "negative" };
  }
  if (connectionStatus === "allowed" && listStatus === "denied") {
    return { icon: "C", label: "Connected; list denied", tone: "warning" };
  }

  const transportFailed = capabilityMetadataBoolean(capabilities, "transport_failed") === true;
  const anyInconclusive = entries.some(([, evidence]) => normalizeCapabilityStatus(evidence.status) === "inconclusive");
  if (transportFailed || anyInconclusive) {
    return {
      icon: "?",
      label: "Assessment inconclusive",
      tone: "warning",
      detail: assessmentReasonDetail(capabilities) || (transportFailed ? "The SMB transport failed before a conclusive result" : undefined),
    };
  }

  const coverage = normalizedMetadataToken(capabilityMetadataString(capabilities, "coverage"));
  const allNotTested = entries.length > 0 && entries.every(([, evidence]) => normalizeCapabilityStatus(evidence.status) === "not_tested");
  if (coverage === "disabled" || allNotTested) {
    return { icon: "—", label: "Not assessed", tone: "neutral", detail: assessmentReasonDetail(capabilities) };
  }

  const finalized = capabilityMetadataBoolean(capabilities, "finalized");
  const complete = capabilityMetadataBoolean(capabilities, "complete");
  if (finalized === false || complete === false) {
    return { icon: "…", label: "Assessment incomplete", tone: "warning", detail: assessmentReasonDetail(capabilities) };
  }
  if (connectionStatus === "allowed") {
    return { icon: "C", label: "Connection observed", tone: "neutral", detail: assessmentReasonDetail(capabilities) };
  }
  if (entries.length > 0) {
    return { icon: "?", label: "No conclusive evidence", tone: "neutral", detail: assessmentReasonDetail(capabilities) };
  }
  return legacyAccessSummary(accessLevel);
}

function humanizeIdentifier(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function capabilityEntries(capabilities: AccessCapabilities | null | undefined): Array<readonly [string, AccessCapabilityEvidence]> {
  if (!capabilities) return [];
  const known = CAPABILITY_ORDER.flatMap((key) => {
    const evidence = capabilityEvidence(capabilities, key);
    return evidence ? [[key, evidence] as const] : [];
  });
  const knownKeys = new Set<string>(CAPABILITY_ORDER);
  const extra = Object.entries(capabilities)
    .flatMap(([key]) => {
      if (key === "_metadata" || knownKeys.has(key)) return [];
      const evidence = capabilityEvidence(capabilities, key);
      return evidence ? [[key, evidence] as const] : [];
    })
    .sort(([left], [right]) => left.localeCompare(right));
  return [...known, ...extra];
}

function formatMetadataValue(value: unknown): string | null {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    const values = value.filter((entry) => typeof entry === "string" || typeof entry === "number").map(String);
    return values.length > 0 ? values.join(", ") : null;
  }
  return null;
}

function evidenceMetadata(evidence: AccessCapabilityEvidence): string[] {
  const metadata: string[] = [];
  for (const [key, value] of Object.entries(evidence)) {
    if (key === "status") continue;
    const normalizedKey = key.toLowerCase();
    if (normalizedKey === "counts" && value && typeof value === "object" && !Array.isArray(value)) {
      for (const [countKey, countValue] of Object.entries(value)) {
        const formatted = formatMetadataValue(countValue);
        if (formatted !== null) metadata.push(`${humanizeIdentifier(countKey)} ${formatted}`);
      }
      continue;
    }
    const directCounter = ["attempted", "allowed", "denied", "inconclusive"].includes(normalizedKey);
    const evidenceDetail = ["scope", "coverage", "reason_code", "protocol_status", "not_tested_reason", "sample_limit"].includes(normalizedKey);
    if (!normalizedKey.includes("count") && !normalizedKey.includes("method") && !directCounter && !evidenceDetail) continue;
    const formatted = formatMetadataValue(value);
    if (directCounter && Number(value) === 0) continue;
    if (formatted !== null) {
      const displayKey = normalizedKey.endsWith("_count") ? normalizedKey.slice(0, -6) : normalizedKey;
      const displayValue = typeof value === "string" && evidenceDetail ? humanizeIdentifier(formatted) : formatted;
      metadata.push(`${humanizeIdentifier(displayKey)} ${displayValue}`);
    }
  }
  return metadata;
}

function capabilityMetadata(capabilities: AccessCapabilities | null | undefined): string[] {
  const metadata = capabilities?._metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return [];

  const preferredOrder = [
    "assessment_summary",
    "assessment_reason",
    "share_presence",
    "probe_method",
    "coverage",
    "sample_count",
    "sampled_count",
    "probe_limit",
    "directory_samples",
    "file_samples",
    "listing_truncated",
    "transport_failed",
    "probes_aborted",
    "probe_abort_reason",
    "degraded",
    "partial",
    "finalized",
    "complete",
  ];
  const entries = Object.entries(metadata).filter(([, value]) => formatMetadataValue(value) !== null);
  entries.sort(([left], [right]) => {
    const leftIndex = preferredOrder.indexOf(left);
    const rightIndex = preferredOrder.indexOf(right);
    if (leftIndex >= 0 || rightIndex >= 0) {
      return (leftIndex < 0 ? preferredOrder.length : leftIndex) - (rightIndex < 0 ? preferredOrder.length : rightIndex);
    }
    return left.localeCompare(right);
  });

  return entries.flatMap(([key, value]) => {
    const formatted = formatMetadataValue(value);
    if (formatted === null) return [];
    const displayValue =
      typeof value === "boolean"
        ? value
          ? "Yes"
          : "No"
        : typeof value === "string" && (["assessment_summary", "assessment_reason", "share_presence", "coverage"].includes(key) || key.includes("method") || key.includes("reason"))
          ? humanizeIdentifier(value)
          : formatted;
    return [`${humanizeIdentifier(key)} ${displayValue}`];
  });
}

export function AccessCapabilityCell({ accessLevel, capabilities, evidenceScope, label, onCopy, onFilter }: AccessCapabilityCellProps) {
  const summary = accessSummary(accessLevel, capabilities);
  const summaryDetail = summary.detail
    || (capabilityMetadataBoolean(capabilities, "degraded") === true ? assessmentReasonDetail(capabilities) : undefined);
  const entries = capabilityEntries(capabilities);
  const probeMetadata = capabilityMetadata(capabilities);
  const observedWriteCapabilities = COMPACT_WRITE_CAPABILITIES.flatMap((key) => {
    const evidence = capabilityEvidence(capabilities, key);
    return evidence && isObserved(evidence) ? [{ key, evidence }] : [];
  });
  const visibleWriteCapabilities = observedWriteCapabilities.slice(0, 1);
  const remainingWriteCapabilityCount = observedWriteCapabilities.length - visibleWriteCapabilities.length;
  const exactValue = accessLevel.trim();
  const compatibilitySummary = legacyAccessSummary(accessLevel);
  const compatibilityValueDiffers = exactValue.length > 0 && compatibilitySummary.label !== summary.label;

  return (
    <div className="inventory-cell inventory-access-cell">
      <div className="inventory-access-content">
        <div className="inventory-access-summary">
          <span
            className={`inventory-access-state is-${summary.tone}`}
            title={`Observed access: ${summary.label}${evidenceScope ? ` (${evidenceScope})` : ""}`}
          >
            <span aria-hidden="true" className="inventory-access-state-icon">{summary.icon}</span>
            {summary.label}
          </span>
          {summaryDetail ? <span className="inventory-access-reason" title={summaryDetail}>{summaryDetail}</span> : null}
          {compatibilityValueDiffers && (onCopy || onFilter) ? (
            <span
              className="inventory-access-compatibility"
              title="Inline filter and copy actions use the stable compatibility access field, not the richer sampled assessment"
            >
              Compatibility value: {humanizeIdentifier(exactValue)}
            </span>
          ) : null}
          {evidenceScope ? (
            <span className="inventory-access-scope" title="Evidence applies at resource scope; object-level coverage depends on the collection method">
              {evidenceScope}
            </span>
          ) : null}
          {visibleWriteCapabilities.map(({ key, evidence }) => {
            const status = normalizeCapabilityStatus(evidence.status);
            return (
              <span className={`inventory-capability-chip is-${status}`} key={key} title={`${CAPABILITY_LABELS[key]}: ${STATUS_PRESENTATION[status].label}`}>
                {CAPABILITY_LABELS[key]}
                {status === "mixed" ? <span aria-label="mixed evidence">±</span> : null}
              </span>
            );
          })}
          {remainingWriteCapabilityCount > 0 ? (
            <span className="inventory-capability-more" title={`${remainingWriteCapabilityCount} more observed capabilities`}>
              +{remainingWriteCapabilityCount}
            </span>
          ) : null}
          {entries.length > 0 || probeMetadata.length > 0 ? (
            <details className="inventory-access-evidence">
              <summary aria-label={`Show ${label.toLowerCase()} evidence`}>Evidence</summary>
              <ul>
                {entries.map(([key, evidence]) => {
                  const status = normalizeCapabilityStatus(evidence.status);
                  const presentation = STATUS_PRESENTATION[status];
                  const metadata = evidenceMetadata(evidence);
                  return (
                    <li className={`is-${status}`} key={key}>
                      <span aria-hidden="true" className="inventory-capability-status-icon">{presentation.icon}</span>
                      <span className="inventory-capability-name">{CAPABILITY_LABELS[key] || humanizeIdentifier(key)}</span>
                      <strong>{presentation.label}</strong>
                      {metadata.length > 0 ? <small>{metadata.join(" · ")}</small> : null}
                    </li>
                  );
                })}
                {probeMetadata.length > 0 ? (
                  <li className="inventory-capability-metadata">
                    <span aria-hidden="true" className="inventory-capability-status-icon">i</span>
                    <span className="inventory-capability-name">Probe details</span>
                    <small>{probeMetadata.join(" · ")}</small>
                  </li>
                ) : null}
              </ul>
            </details>
          ) : null}
        </div>
      </div>

      {exactValue && (onCopy || onFilter) ? (
        <>
          <span className="inventory-cell-actions">
            {onFilter ? (
              <>
                <button
                  aria-label={`Filter all results where the compatibility access value exactly matches ${exactValue}`}
                  onClick={() => onFilter(exactValue, false)}
                  title={`Filter compatibility access value: ${exactValue}`}
                  type="button"
                >
                  =
                </button>
                <button
                  aria-label={`Exclude all results where the compatibility access value exactly matches ${exactValue}`}
                  onClick={() => onFilter(exactValue, true)}
                  title={`Exclude compatibility access value: ${exactValue}`}
                  type="button"
                >
                  ≠
                </button>
              </>
            ) : null}
            {onCopy ? (
              <button aria-label="Copy compatibility access value" onClick={() => onCopy(exactValue, `${label} value`)} title="Copy compatibility access value" type="button">
                Copy
              </button>
            ) : null}
          </span>
          <select
            aria-label={`Actions for ${label}`}
            className="inventory-cell-menu"
            defaultValue=""
            onChange={(event) => {
              const action = event.currentTarget.value;
              event.currentTarget.value = "";
              if (action === "filter" && onFilter) onFilter(exactValue, false);
              if (action === "exclude" && onFilter) onFilter(exactValue, true);
              if (action === "copy" && onCopy) onCopy(exactValue, `${label} value`);
            }}
            title={`Actions for ${label}`}
          >
            <option disabled value="">•••</option>
            {onFilter ? <option value="filter">Filter this value</option> : null}
            {onFilter ? <option value="exclude">Exclude this value</option> : null}
            {onCopy ? <option value="copy">Copy exact value</option> : null}
          </select>
        </>
      ) : null}
    </div>
  );
}
