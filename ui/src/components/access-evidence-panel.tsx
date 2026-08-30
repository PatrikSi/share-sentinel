import { useEffect, useMemo, useRef, useState } from "react";

import { AccessCapabilityCell, type AccessCapabilities } from "@/components/access-capability-cell";
import { EffectiveAccessAnalysisSection } from "@/components/effective-access-analysis";
import { CollectionContextPanel } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  directPermissionEntryLabel,
  directPermissionPrincipalDetail,
  directPermissionEntryRights,
  evidenceErrorText,
  evidenceTone,
  humanizeEvidenceValue,
  isDirectPermissionAssessment,
  mergeAccessEvidenceDetails,
  presentAccessEvidence,
  type AccessEvidenceAssessment,
  type AccessEvidenceDetail,
  type AccessEvidenceProvenance,
} from "@/lib/access-evidence";
import { useModalPanel } from "@/lib/use-modal-panel";
import type { CollectionContext } from "@/lib/provider-context";

const ASSESSMENT_PAGE_LIMIT = "25";
const ENTRY_PAGE_LIMIT = "100";

type AccessEvidencePanelProps = {
  projectId: string;
  runId: string;
  resourceId: number;
  resourceName: string;
  onClose: () => void;
};

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function assessmentTitle(assessment: AccessEvidenceAssessment): string {
  return (
    assessment.label?.trim() ||
    humanizeEvidenceValue(assessment.semantics || assessment.kind || "access_observation")
  );
}

function assessmentState(assessment: AccessEvidenceAssessment): string {
  return assessment.assessment_state || assessment.state || "not_assessed";
}

function formatEvidenceTimestamp(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function assessmentSummaryText(assessment: AccessEvidenceAssessment): string | null {
  if (typeof assessment.summary === "string") return assessment.summary;
  if (!assessment.summary) return null;
  for (const key of ["description", "message", "label", "assessment_summary"]) {
    const value = assessment.summary[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function assessmentCoverageFacts(assessment: AccessEvidenceAssessment): Array<{ label: string; value: string }> {
  if (typeof assessment.coverage === "string") {
    return [{ label: "Coverage", value: humanizeEvidenceValue(assessment.coverage) }];
  }
  const coverage = assessment.coverage;
  if (!coverage) return [{ label: "Coverage", value: "Not recorded" }];
  return [
    { label: "Selection scope", value: humanizeEvidenceValue(coverage.selection_scope) },
    { label: "Selection", value: humanizeEvidenceValue(coverage.selection) },
    { label: "Retrieval", value: humanizeEvidenceValue(coverage.retrieval) },
    { label: "Provider visibility", value: humanizeEvidenceValue(coverage.provider_visibility) },
    { label: "Semantics", value: humanizeEvidenceValue(coverage.semantic) },
    { label: "Principal resolution", value: humanizeEvidenceValue(coverage.principal_resolution) },
    { label: "Effective access", value: humanizeEvidenceValue(coverage.effective_access) },
  ];
}

function negativeConclusionSupported(assessment: AccessEvidenceAssessment): boolean | null {
  if (assessment.authoritative === true) return true;
  if (assessment.authoritative === false) return false;
  if (typeof assessment.coverage === "object" && assessment.coverage) {
    return assessment.coverage.negative_conclusion_supported ?? null;
  }
  return null;
}

function provenanceRows(provenance: AccessEvidenceProvenance | null | undefined) {
  if (!provenance) return [];
  const fields: Array<[keyof AccessEvidenceProvenance, string]> = [
    ["provider", "Provider"],
    ["method", "Method"],
    ["assessed_identity", "Assessed identity"],
    ["collected_at", "Collected at"],
    ["collector_version", "Collector version"],
    ["artifact_schema_version", "Artifact schema"],
    ["run_id", "Run ID"],
    ["run_name", "Run name"],
    ["run_created_at", "Run created at"],
    ["source", "Source"],
  ];
  return fields.flatMap(([key, label]) => {
    const value = provenance[key];
    if (value === null || value === undefined || value === "") return [];
    const text = (key === "collected_at" || key === "run_created_at") && typeof value === "string"
      ? Number.isNaN(new Date(value).getTime())
        ? value
        : new Date(value).toLocaleString()
      : String(value);
    return [{ label, value: text }];
  });
}

function normalizeAccessEvidenceDetail(data: unknown): AccessEvidenceDetail {
  const record = (data || {}) as Partial<AccessEvidenceDetail>;
  return {
    resource: record.resource || null,
    overall: record.overall || { assessment_state: "not_assessed", outcome: "not_assessed" },
    assessments: Array.isArray(record.assessments) ? record.assessments : [],
    provenance: record.provenance || null,
  };
}

function AssessmentBlock({ assessment }: { assessment: AccessEvidenceAssessment }) {
  const claims = assessment.claims || [];
  const limitations = assessment.limitations || [];
  const errors = [
    ...(assessment.errors || []),
    ...(assessment.error_code && !(assessment.errors || []).some((error) => evidenceErrorText(error).includes(assessment.error_code || ""))
      ? [assessment.error_code]
      : []),
  ];
  const state = assessmentState(assessment);
  const tone = evidenceTone(assessment.outcome || (state === "complete" ? "observed" : "not_assessed"), state);
  const provenance = provenanceRows(assessment.provenance);
  const summary = assessmentSummaryText(assessment);
  const coverageFacts = assessmentCoverageFacts(assessment);
  const negativeSupported = negativeConclusionSupported(assessment);

  return (
    <article className="access-evidence-assessment">
      <header>
        <div>
          <h4>{assessmentTitle(assessment)}</h4>
          {summary ? <p>{summary}</p> : null}
        </div>
        <div className="access-evidence-assessment-badges">
          <span className={`is-${tone}`}>{humanizeEvidenceValue(assessment.outcome || "not_assessed")}</span>
          <span>{humanizeEvidenceValue(state)}</span>
        </div>
      </header>
      <dl className="access-evidence-inline-facts">
        <div>
          <dt>Scope</dt>
          <dd>{humanizeEvidenceValue(assessment.scope || assessment.subject?.kind || (typeof assessment.coverage === "object" ? assessment.coverage?.selection_scope : null))}</dd>
        </div>
        {coverageFacts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
        <div>
          <dt>Negative conclusion</dt>
          <dd>{negativeSupported === true ? "Supported for scope" : negativeSupported === false ? "Not supported" : "Not recorded"}</dd>
        </div>
      </dl>
      {claims.length > 0 ? (
        <ul aria-label={`${assessmentTitle(assessment)} claims`} className="access-evidence-claims">
          {claims.map((claim, index) => {
            const label = claim.label || claim.capability || claim.kind || "Evidence claim";
            const outcome = claim.outcome || claim.status || (claim.value == null ? "not_assessed" : String(claim.value));
            const reason = claim.detail || claim.reason || claim.reason_code;
            return (
              <li key={claim.id ?? `${label}-${index}`}>
                <div>
                  <strong>{humanizeEvidenceValue(label)}</strong>
                  {reason ? <small>{reason}</small> : null}
                  {claim.method || claim.scope ? (
                    <small>{[claim.method && `Method: ${humanizeEvidenceValue(claim.method)}`, claim.scope && `Scope: ${humanizeEvidenceValue(claim.scope)}`].filter(Boolean).join(" · ")}</small>
                  ) : null}
                </div>
                <span className={`is-${evidenceTone(outcome)}`}>{humanizeEvidenceValue(outcome)}</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="access-evidence-empty-detail">No individual capability claims were recorded for this assessment.</p>
      )}
      {limitations.length > 0 ? (
        <div className="access-evidence-limitations">
          <strong>Limitations</strong>
          <ul>{limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
        </div>
      ) : null}
      {errors.length > 0 ? (
        <div className="access-evidence-errors" role="status">
          <strong>Collection errors</strong>
          <ul>{errors.map((error, index) => <li key={`${evidenceErrorText(error)}-${index}`}>{evidenceErrorText(error)}</li>)}</ul>
        </div>
      ) : null}
      {provenance.length > 0 ? (
        <details className="access-evidence-provenance-disclosure">
          <summary>Assessment provenance</summary>
          <dl>{provenance.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
        </details>
      ) : null}
    </article>
  );
}

function DirectPermissions({ assessments, assessed }: { assessments: AccessEvidenceAssessment[]; assessed: boolean }) {
  if (assessments.length === 0) {
    return (
      <StatePanel
        description={assessed
          ? "The summary reports a provider permission assessment, but permission entries were not included in this response. Do not infer that no permissions exist."
          : "This collection did not enumerate provider ACL or sharing entries. Observed protocol capabilities and visibility are shown separately below."}
        title={assessed ? "Provider permission evidence details unavailable" : "Provider permission evidence not assessed"}
      />
    );
  }

  return (
    <div className="access-evidence-direct-list">
      {assessments.map((assessment, assessmentIndex) => {
        const entries = assessment.entries || [];
        const state = assessmentState(assessment);
        const authoritativeEmpty = state === "complete"
          && negativeConclusionSupported(assessment) === true
          && assessment.counts?.observed === 0
          && assessment.counts?.emitted === 0;
        const summary = assessmentSummaryText(assessment);
        const coverageFacts = assessmentCoverageFacts(assessment);
        const subjectReference = assessment.subject?.path
          || assessment.subject?.provider_id
          || assessment.subject?.key
          || (assessment.subject?.item_id == null ? null : `Item ${assessment.subject.item_id}`);
        const subjectLabel = [
          assessment.subject?.kind ? humanizeEvidenceValue(assessment.subject.kind) : null,
          subjectReference,
        ].filter(Boolean).join(" · ");
        const assessmentErrors = [
          ...(assessment.errors || []),
          ...(assessment.error_code && !(assessment.errors || []).some((error) => evidenceErrorText(error).includes(assessment.error_code || ""))
            ? [assessment.error_code]
            : []),
        ];
        return (
          <article className="access-evidence-assessment" key={assessment.id || `direct-${assessmentIndex}`}>
            <header>
              <div>
                <h4>{assessmentTitle(assessment)}</h4>
                <p>{summary || "Provider-declared permission entries collected for this resource."}</p>
                {subjectLabel ? (
                  <p className="access-evidence-assessment-subject" title={subjectLabel}>
                    <strong>Assessed object</strong>
                    <span>{subjectLabel}</span>
                  </p>
                ) : null}
              </div>
              <div className="access-evidence-assessment-badges">
                <span className={`is-${evidenceTone(assessment.outcome || (state === "complete" ? "observed" : "not_assessed"), state)}`}>
                  {humanizeEvidenceValue(state)}
                </span>
              </div>
            </header>
            {entries.length > 0 ? (
              <ul aria-label="Provider permission entries" className="access-evidence-permission-entries">
                {entries.map((entry, index) => (
                  <li key={entry.id ?? entry.source_permission_id ?? `${directPermissionEntryLabel(entry)}-${index}`}>
                    <div>
                      <strong>{directPermissionEntryLabel(entry)}</strong>
                      <small title={directPermissionPrincipalDetail(entry)}>{directPermissionPrincipalDetail(entry)}</small>
                    </div>
                    <div>
                      <strong>{directPermissionEntryRights(entry)}</strong>
                      <small>
                        {[
                          entry.inherited_state
                            ? humanizeEvidenceValue(entry.inherited_state)
                            : entry.inherited == null ? null : entry.inherited ? "Inherited" : "Direct",
                          (entry.link_scope || (typeof entry.provider_details?.link_scope === "string" ? entry.provider_details.link_scope : null))
                            ? `Link: ${humanizeEvidenceValue(entry.link_scope || String(entry.provider_details?.link_scope || ""))}`
                            : null,
                          (entry.expires_at || entry.expiration_at) ? `Expires ${new Date(entry.expires_at || entry.expiration_at || "").toLocaleString()}` : null,
                        ].filter(Boolean).join(" · ") || "Permission metadata not recorded"}
                      </small>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="access-evidence-empty-detail">
                {authoritativeEmpty
                  ? "No provider permission entries were recorded within this assessment's authoritative scope."
                  : (assessment.counts?.emitted || 0) > 0
                    ? `${(assessment.counts?.emitted || 0).toLocaleString()} permission entries were recorded, but they are not included in this response page.`
                    : "No provider permission entries are shown. Coverage and counts are not authoritative enough to conclude that none exist."}
              </p>
            )}
            <dl className="access-evidence-inline-facts">
              {subjectLabel ? (
                <div className="is-subject">
                  <dt>Assessed object</dt>
                  <dd title={subjectLabel}>{subjectLabel}</dd>
                </div>
              ) : null}
              {assessment.subject?.kind ? <div><dt>Subject type</dt><dd>{humanizeEvidenceValue(assessment.subject.kind)}</dd></div> : null}
              {assessment.scope ? <div><dt>Assessment scope</dt><dd>{humanizeEvidenceValue(assessment.scope)}</dd></div> : null}
              {coverageFacts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
              <div><dt>Entries</dt><dd>{assessment.counts?.emitted == null ? "Not recorded" : `${assessment.counts.emitted.toLocaleString()} emitted / ${assessment.counts.observed?.toLocaleString() ?? "unknown"} observed`}</dd></div>
              <div><dt>Negative conclusion</dt><dd>{negativeConclusionSupported(assessment) === true ? "Supported for scope" : "Not supported"}</dd></div>
              <div><dt>Observed at</dt><dd>{formatEvidenceTimestamp(assessment.observed_at)}</dd></div>
            </dl>
            {(assessment.limitations || []).length > 0 ? (
              <div className="access-evidence-limitations"><strong>Limitations</strong><ul>{assessment.limitations?.map((value) => <li key={value}>{value}</li>)}</ul></div>
            ) : null}
            {assessmentErrors.length > 0 ? (
              <div className="access-evidence-errors" role="status"><strong>Collection errors</strong><ul>{assessmentErrors.map((value, index) => <li key={`${evidenceErrorText(value)}-${index}`}>{evidenceErrorText(value)}</li>)}</ul></div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

export function AccessEvidencePanel({
  projectId,
  runId,
  resourceId,
  resourceName,
  onClose,
}: AccessEvidencePanelProps) {
  const panelRef = useModalPanel<HTMLElement>(onClose, `${projectId}:${runId}:${resourceId}`);
  const [detail, setDetail] = useState<AccessEvidenceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [assessmentPageCursor, setAssessmentPageCursor] = useState<number | null>(null);
  const loadMoreControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setDetail(null);
    setLoadingMore(false);
    setLoadMoreError(null);
    setAssessmentPageCursor(null);
    loadMoreControllerRef.current?.abort();
    apiFetch(
      `/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/resources/${resourceId}/access-evidence`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (controller.signal.aborted) return;
        setDetail(normalizeAccessEvidenceDetail(data));
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) {
          setError(caught instanceof Error ? caught.message : "Access evidence could not be loaded.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      loadMoreControllerRef.current?.abort();
    };
  }, [projectId, reloadNonce, resourceId, runId]);

  async function loadMoreEvidence() {
    const pagination = detail?.provenance?.pagination;
    if (!pagination || loadingMore) return;
    const loadEntries = pagination.entries_truncated === true && pagination.next_entry_id != null;
    const loadAssessments = !loadEntries
      && pagination.assessments_truncated === true
      && pagination.next_assessment_id != null;
    if (!loadEntries && !loadAssessments) return;

    const params = new URLSearchParams({
      assessment_limit: ASSESSMENT_PAGE_LIMIT,
      entry_limit: ENTRY_PAGE_LIMIT,
    });
    let nextAssessmentPageCursor = assessmentPageCursor;
    if (loadEntries) {
      params.set("after_entry_id", String(pagination.next_entry_id));
      if (assessmentPageCursor != null) params.set("after_assessment_id", String(assessmentPageCursor));
    } else if (pagination.next_assessment_id != null) {
      nextAssessmentPageCursor = pagination.next_assessment_id;
      params.set("after_assessment_id", String(pagination.next_assessment_id));
    }

    loadMoreControllerRef.current?.abort();
    const controller = new AbortController();
    loadMoreControllerRef.current = controller;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const data = await apiFetch(
        `/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/resources/${resourceId}/access-evidence?${params.toString()}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const incoming = normalizeAccessEvidenceDetail(data);
      setDetail((current) => current ? mergeAccessEvidenceDetails(current, incoming) : incoming);
      if (loadAssessments) setAssessmentPageCursor(nextAssessmentPageCursor);
    } catch (caught) {
      if (!controller.signal.aborted && !isAbortError(caught)) {
        setLoadMoreError(caught instanceof Error ? caught.message : "Additional access evidence could not be loaded.");
      }
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (loadMoreControllerRef.current === controller) loadMoreControllerRef.current = null;
    }
  }

  const directAssessments = useMemo(
    () => (detail?.assessments || []).filter(isDirectPermissionAssessment),
    [detail?.assessments],
  );
  const observedAssessments = useMemo(
    () => (detail?.assessments || []).filter((assessment) => !isDirectPermissionAssessment(assessment)),
    [detail?.assessments],
  );
  const presentation = presentAccessEvidence(detail?.overall);
  const rootProvenance = provenanceRows(detail?.provenance);
  const title = detail?.resource?.name || resourceName;
  const accessCapabilities = detail?.overall.access_capabilities;
  const hasAccessCapabilities = !!accessCapabilities && Object.keys(accessCapabilities).length > 0;
  const evidencePagination = detail?.provenance?.pagination;
  const loadedDirectEntryCount = directAssessments.reduce((total, assessment) => total + (assessment.entries || []).length, 0);
  const moreEntriesAvailable = evidencePagination?.entries_truncated === true && evidencePagination.next_entry_id != null;
  const moreAssessmentsAvailable = evidencePagination?.assessments_truncated === true && evidencePagination.next_assessment_id != null;
  const moreEvidenceAvailable = moreEntriesAvailable || moreAssessmentsAvailable;
  const evidencePartial = detail?.overall.assessment_state === "partial"
    || detail?.overall.status === "partial"
    || detail?.overall.partial === true
    || detail?.overall.direct_permissions?.partial === true
    || detail?.overall.capability_observations?.partial === true;

  return (
    <div
      className="access-evidence-layer"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside aria-labelledby="access-evidence-panel-title" aria-modal="true" className="access-evidence-panel" ref={panelRef} role="dialog" tabIndex={-1}>
        <header className="access-evidence-panel-header">
          <div>
            <span>Access evidence</span>
            <h2 id="access-evidence-panel-title">{title}</h2>
            <p>Collected evidence for this run; not a live check or an effective-permissions calculation.</p>
          </div>
          <button aria-label="Close access evidence" onClick={onClose} type="button">Close</button>
        </header>

        <div aria-busy={loading} className="access-evidence-panel-body">
          {loading ? (
            <div aria-live="polite" className="access-evidence-panel-loading" role="status">
              <span />
              <span />
              <span />
              <p>Loading normalized evidence…</p>
            </div>
          ) : null}
          {error ? (
            <StatusBanner tone="error" title="Access evidence unavailable">
              <p>{error}</p>
              <p className="mt-1">No evidence detail is being shown. Retrying this read is safe.</p>
              <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry</button>
            </StatusBanner>
          ) : null}
          {detail && !loading ? (
            <>
              <section aria-labelledby="access-evidence-overall-title" className="access-evidence-overall">
                <div>
                  <h3 id="access-evidence-overall-title">Assessment summary</h3>
                  <p>{presentation.detail}</p>
                </div>
                <div className="access-evidence-overall-badges">
                  <span className={`is-${presentation.tone}`}>{presentation.label}</span>
                  <span>{presentation.stateLabel}</span>
                  {presentation.coverageLabel ? <span>{presentation.coverageLabel} provider-permission scope</span> : null}
                </div>
              </section>

              {evidencePartial ? (
                <StatusBanner tone="warning" title="Evidence is partial">
                  <p>Some checks, resources, or identities were not assessed. Missing observations must not be interpreted as denied access.</p>
                </StatusBanner>
              ) : null}

              <section aria-labelledby="direct-permissions-title" className="access-evidence-section">
                <div className="access-evidence-section-heading">
                  <div>
                    <h3 id="direct-permissions-title">Provider permission evidence</h3>
                    <p>Filesystem ACL records or caller-visible Graph sharing permission records. These remain separate from observed capabilities and are not an effective-access calculation.</p>
                  </div>
                  <span>
                    {presentation.directPermissionsLabel}
                    {(evidencePagination?.entries_truncated || evidencePagination?.assessments_truncated)
                      ? ` · ${loadedDirectEntryCount.toLocaleString()} loaded`
                      : ""}
                  </span>
                </div>
                <DirectPermissions assessments={directAssessments} assessed={presentation.directPermissionsAssessed} />
              </section>

              <section aria-labelledby="observed-capabilities-title" className="access-evidence-section">
                <div className="access-evidence-section-heading">
                  <div>
                    <h3 id="observed-capabilities-title">Observed capabilities and visibility</h3>
                    <p>Protocol operations, metadata enumeration, visibility, and exposure recorded during collection.</p>
                  </div>
                  <span>{observedAssessments.length.toLocaleString()} assessment{observedAssessments.length === 1 ? "" : "s"}</span>
                </div>
                {hasAccessCapabilities ? (
                  <article className="access-evidence-assessment">
                    <header><div><h4>Protocol capability observations</h4><p>Bounded operations recorded by the collector. These are not ACL entries or an effective-access calculation.</p></div></header>
                    <AccessCapabilityCell
                      accessLevel={detail.overall.access_level || "unknown"}
                      capabilities={accessCapabilities as AccessCapabilities}
                      evidenceScope="Collection-time scope"
                      label="Observed compatibility access"
                    />
                  </article>
                ) : null}
                {observedAssessments.length > 0 ? observedAssessments.map((assessment, index) => (
                  <AssessmentBlock assessment={assessment} key={assessment.id || `${assessment.kind || "assessment"}-${index}`} />
                )) : !hasAccessCapabilities ? (
                  <StatePanel
                    description="The collector did not record a normalized capability or visibility assessment for this resource. Compatibility access may still exist on legacy rows."
                    title="Observed capabilities not assessed"
                  />
                ) : null}
              </section>

              <EffectiveAccessAnalysisSection projectId={projectId} resourceId={resourceId} runId={runId} />

              <section aria-labelledby="evidence-provenance-title" className="access-evidence-section">
                <div className="access-evidence-section-heading">
                  <div>
                    <h3 id="evidence-provenance-title">Coverage and provenance</h3>
                    <p>Use this context before treating an observation as representative of the resource.</p>
                  </div>
                </div>
                {rootProvenance.length > 0 ? (
                  <dl className="access-evidence-provenance">
                    {rootProvenance.map((row) => <div key={row.label}><dt>{row.label}</dt><dd title={row.value}>{row.value}</dd></div>)}
                  </dl>
                ) : (
                  <p className="access-evidence-empty-detail">Run-level provenance was not recorded in this response. Assessment-level provenance may still be available above.</p>
                )}
                <CollectionContextPanel
                  compact
                  context={(detail.provenance?.collection_context || null) as CollectionContext | null}
                />
                {(evidencePagination?.assessments_truncated || evidencePagination?.entries_truncated) ? (
                  <StatusBanner tone="warning" title="Evidence response is paginated">
                    <p>
                      {evidencePagination.assessments_truncated ? "Additional assessments were not loaded. " : ""}
                      {evidencePagination.entries_truncated ? "Additional permission entries are available." : ""}
                    </p>
                    {moreEvidenceAvailable && !loadMoreError ? (
                      <button
                        className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold"
                        disabled={loadingMore}
                        onClick={loadMoreEvidence}
                        type="button"
                      >
                        {loadingMore
                          ? "Loading more evidence…"
                          : moreEntriesAvailable
                            ? "Load more permission entries"
                            : "Load more assessments"}
                      </button>
                    ) : !moreEvidenceAvailable ? (
                      <p className="mt-1">The API did not provide a continuation cursor for the remaining evidence.</p>
                    ) : null}
                  </StatusBanner>
                ) : null}
                {loadMoreError ? (
                  <StatusBanner tone="error" title="Additional evidence could not be loaded">
                    <p>{loadMoreError} Evidence already loaded remains visible.</p>
                    {moreEvidenceAvailable ? <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={loadMoreEvidence} type="button">Retry loading more</button> : null}
                  </StatusBanner>
                ) : null}
              </section>
            </>
          ) : null}
        </div>
      </aside>
    </div>
  );
}
