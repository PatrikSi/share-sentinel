import type { SharePointAssessment } from "@/lib/inventory-assessment";

type SharePointAssessmentCellProps = {
  assessment: SharePointAssessment;
  label: string;
  onDetails: () => void;
  onViewItems?: () => void;
};

export function SharePointAssessmentCell({ assessment, label, onDetails, onViewItems }: SharePointAssessmentCellProps) {
  const content = assessment.content;
  const countSummary = [
    assessment.fileCount === null ? null : `${assessment.fileCount.toLocaleString()} file${assessment.fileCount === 1 ? "" : "s"}`,
    assessment.folderCount === null ? null : `${assessment.folderCount.toLocaleString()} folder${assessment.folderCount === 1 ? "" : "s"}`,
  ].filter((value): value is string => value !== null).join(" · ");

  return (
    <div className="inventory-assessment-cell">
      <div className="inventory-assessment-badges">
        <span
          className={`inventory-assessment-badge is-${assessment.availability.tone}`}
          title={assessment.availability.detail}
        >
          {assessment.availability.label}
        </span>
        <span
          className={`inventory-assessment-badge is-${assessment.lifecycle.tone}`}
          title={assessment.lifecycle.detail}
        >
          {assessment.lifecycle.label}
        </span>
      </div>
      {content ? (
        <span className={`inventory-assessment-content is-${content.tone}`} title={content.detail}>
          {content.label}{countSummary ? ` · ${countSummary}` : ""}
        </span>
      ) : assessment.availability.key === "confirmed" && assessment.itemCount !== null ? (
        <span className="inventory-assessment-content is-neutral">
          {assessment.itemCount.toLocaleString()} collected item{assessment.itemCount === 1 ? "" : "s"}
        </span>
      ) : null}
      <span className="inventory-assessment-actions">
        <button aria-label={`Show assessment details for ${label}`} onClick={onDetails} type="button">
          Details
        </button>
        {assessment.canViewItems && onViewItems ? (
          <button aria-label={`View collected files and folders for ${label}`} onClick={onViewItems} type="button">
            View items
          </button>
        ) : null}
      </span>
    </div>
  );
}
