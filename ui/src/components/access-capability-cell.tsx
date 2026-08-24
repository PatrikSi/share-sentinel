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
  label:
    | "Read/write observed"
    | "Read observed"
    | "List/write observed"
    | "List observed"
    | "Write observed"
    | "Control observed"
    | "Access observed"
    | "Access denied"
    | "Unknown";
  tone: "positive" | "warning" | "negative" | "neutral";
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
    return { icon: "RW", label: "Read/write observed", tone: "positive" };
  }
  if (readObserved) {
    return { icon: "R", label: "Read observed", tone: "positive" };
  }
  if (listObserved && writeObserved) {
    return { icon: "LW", label: "List/write observed", tone: "positive" };
  }
  if (writeObserved) {
    return { icon: "W", label: "Write observed", tone: "positive" };
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

  const connectionDenied = normalizeCapabilityStatus(capabilityEvidence(capabilities, "tree_connect")?.status) === "denied";
  if (connectionDenied) {
    return { icon: "×", label: "Access denied", tone: "negative" };
  }
  if (entries.length > 0) {
    return { icon: "?", label: "Unknown", tone: "neutral" };
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
    if (!normalizedKey.includes("count") && !normalizedKey.includes("method") && !directCounter) continue;
    const formatted = formatMetadataValue(value);
    if (directCounter && Number(value) === 0) continue;
    if (formatted !== null) {
      const displayKey = normalizedKey.endsWith("_count") ? normalizedKey.slice(0, -6) : normalizedKey;
      metadata.push(`${humanizeIdentifier(displayKey)} ${formatted}`);
    }
  }
  return metadata;
}

function capabilityMetadata(capabilities: AccessCapabilities | null | undefined): string[] {
  const metadata = capabilities?._metadata;
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return [];

  const preferredOrder = ["probe_method", "coverage", "sample_count", "sampled_count", "probe_limit", "partial", "complete"];
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
        : typeof value === "string" && (key.includes("method") || key === "coverage")
          ? humanizeIdentifier(value)
          : formatted;
    return [`${humanizeIdentifier(key)} ${displayValue}`];
  });
}

export function AccessCapabilityCell({ accessLevel, capabilities, evidenceScope, label, onCopy, onFilter }: AccessCapabilityCellProps) {
  const summary = accessSummary(accessLevel, capabilities);
  const entries = capabilityEntries(capabilities);
  const probeMetadata = capabilityMetadata(capabilities);
  const observedWriteCapabilities = COMPACT_WRITE_CAPABILITIES.flatMap((key) => {
    const evidence = capabilityEvidence(capabilities, key);
    return evidence && isObserved(evidence) ? [{ key, evidence }] : [];
  });
  const visibleWriteCapabilities = observedWriteCapabilities.slice(0, 1);
  const remainingWriteCapabilityCount = observedWriteCapabilities.length - visibleWriteCapabilities.length;
  const exactValue = accessLevel.trim();

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
          {evidenceScope ? (
            <span className="inventory-access-scope" title="Capability evidence applies to the sampled share, not necessarily this exact item">
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
      ) : null}
    </div>
  );
}
