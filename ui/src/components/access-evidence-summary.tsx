import {
  presentAccessEvidence,
  type AccessEvidenceSummary,
} from "@/lib/access-evidence";

type AccessEvidenceSummaryCellProps = {
  summary?: AccessEvidenceSummary | null;
  resourceName: string;
  compatibilityAccess?: string | null;
  onOpen: () => void;
  onFilterCompatibility?: (value: string, negated: boolean) => void;
};

export function AccessEvidenceSummaryCell({
  summary,
  resourceName,
  compatibilityAccess,
  onOpen,
  onFilterCompatibility,
}: AccessEvidenceSummaryCellProps) {
  const presentation = presentAccessEvidence(summary);
  const normalizedCompatibility = compatibilityAccess?.trim() || "";

  return (
    <div className="access-evidence-summary-cell">
      <button
        aria-label={`Open access evidence for ${resourceName}`}
        className="access-evidence-summary-trigger"
        onClick={onOpen}
        type="button"
      >
        <span className={`access-evidence-summary-state is-${presentation.tone}`}>{presentation.label}</span>
        <span className="access-evidence-summary-meta">{presentation.stateLabel}</span>
        <span className="access-evidence-summary-permissions">
          {presentation.directPermissionsLabel}
          {presentation.coverageLabel ? ` · ${presentation.coverageLabel}` : ""}
        </span>
        <span className="access-evidence-summary-action">Inspect</span>
      </button>
      {normalizedCompatibility && onFilterCompatibility ? (
        <div className="access-evidence-compatibility-actions">
          <span title="Legacy access classification retained for inventory filtering">
            Filter: {normalizedCompatibility.replaceAll("_", " ")}
          </span>
          <button
            aria-label={`Filter resources where compatibility access exactly matches ${normalizedCompatibility}`}
            onClick={() => onFilterCompatibility(normalizedCompatibility, false)}
            title="Filter by compatibility access"
            type="button"
          >
            =
          </button>
          <button
            aria-label={`Exclude resources where compatibility access exactly matches ${normalizedCompatibility}`}
            onClick={() => onFilterCompatibility(normalizedCompatibility, true)}
            title="Exclude compatibility access"
            type="button"
          >
            ≠
          </button>
        </div>
      ) : null}
    </div>
  );
}
