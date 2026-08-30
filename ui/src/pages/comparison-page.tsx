import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ComparisonItemChanges } from "@/components/comparison-item-changes";
import { ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import { humanizeEvidenceValue, presentAccessEvidence } from "@/lib/access-evidence";
import {
  changeSnapshot,
  changeTypeLabel,
  changeTypeTone,
  canRetryComparisonFindings,
  canRetryMaterializedComparison,
  comparisonCompatibilityTone,
  comparisonErrorText,
  emptyResourceChangesDescription,
  comparisonFindingsEvaluation,
  comparisonRunId,
  comparisonRunLabel,
  comparisonStateTone,
  comparisonSummaryCounts,
  itemChangeCopy,
  normalizeChangeType,
  resourceChangeKey,
  resourceChangeCategories,
  resourceChangeName,
  resourceChangeProvider,
  type ProjectComparison,
  type ResourceChangePage,
  type ResourceChangeType,
  type ResourceComparisonChange,
  type ResourceComparisonSnapshot,
} from "@/lib/comparisons";
import {
  formatMonitoringTimestamp,
  monitoringEvaluationIsActive,
  monitoringEvaluationState,
  safeMonitoringCount,
  safeMonitoringDiagnostic,
} from "@/lib/monitoring";
import { useModalPanel } from "@/lib/use-modal-panel";

const PAGE_LIMIT = 100;
const COMPARISON_CATEGORY_OPTIONS = [
  "location",
  "access",
  "permission_evidence",
  "item_count",
  "structure_not_comparable",
  "access_not_comparable",
  "permission_evidence_not_comparable",
  "exposure_not_comparable",
  "item_count_not_comparable",
] as const;

function readSearchParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function formatCount(value: number | null | undefined): string {
  return value == null ? "Not computed" : value.toLocaleString();
}

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "Not recorded";
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function runTimestamp(run: ProjectComparison["current_run"]): string | null {
  if (!run?.created_at) return null;
  const date = new Date(run.created_at);
  return Number.isNaN(date.getTime()) ? run.created_at : date.toLocaleString();
}

function comparisonStateCopy(comparison: ProjectComparison): string {
  if (comparison.state === "queued") {
    if (comparison.next_retry_at) {
      const retryAt = new Date(comparison.next_retry_at);
      const retryLabel = Number.isNaN(retryAt.getTime()) ? comparison.next_retry_at : retryAt.toLocaleString();
      return `A retry is scheduled for ${retryLabel}. This page will update automatically.`;
    }
    return "Waiting for comparison capacity. This page will update automatically.";
  }
  if (comparison.state === "running") {
    const processed = comparison.progress?.processed;
    const total = comparison.progress?.total;
    if (processed != null && total != null) return `${processed.toLocaleString()} of ${total.toLocaleString()} resources processed. This page will update automatically.`;
    return comparison.progress?.message || "Materializing resource changes. This page will update automatically.";
  }
  if (comparison.state === "failed") return comparisonErrorText(comparison.error) || "The comparison failed before a result could be published.";
  return "Materialized resource comparison is ready.";
}

function snapshotEvidenceLabel(snapshot: ResourceComparisonSnapshot | null | undefined): string {
  if (!snapshot) return "Not present";
  if (snapshot.access_evidence_summary || snapshot.permission_summary) return presentAccessEvidence(snapshot.access_evidence_summary || snapshot.permission_summary).label;
  return snapshot.access_level ? humanizeEvidenceValue(snapshot.access_level) : "Not assessed";
}

function evidenceInventoryLink(projectId: string, snapshot: ResourceComparisonSnapshot | null | undefined): string | null {
  if (!snapshot?.run_id || !snapshot.resource_id) return null;
  const params = new URLSearchParams({
    tab: "resources",
    runs: snapshot.run_id,
    evidenceRun: snapshot.run_id,
    evidenceResource: String(snapshot.resource_id),
  });
  return `/projects/${projectId}/inventory?${params.toString()}`;
}

function SnapshotCard({ label, projectId, snapshot }: { label: string; projectId: string; snapshot: ResourceComparisonSnapshot | null | undefined }) {
  if (!snapshot) {
    return (
      <section className="comparison-snapshot is-empty">
        <h4>{label}</h4>
        <p>This resource was not present in this side of the comparison.</p>
      </section>
    );
  }
  const evidenceLink = evidenceInventoryLink(projectId, snapshot);
  const facts = [
    ["Resource", snapshot.name || "Unnamed resource"],
    ["Endpoint / site", snapshot.hostname || snapshot.endpoint_key || "Not recorded"],
    ["Provider ID", snapshot.provider_resource_id || "Not recorded"],
    ["Resource type", humanizeEvidenceValue(snapshot.resource_type)],
    ["Access evidence", snapshotEvidenceLabel(snapshot)],
    ["Compatibility access", humanizeEvidenceValue(snapshot.access_level)],
    ["Exposure", humanizeEvidenceValue(snapshot.exposure)],
    ["Lifecycle", humanizeEvidenceValue(snapshot.lifecycle_state)],
    ["Items", formatCount(snapshot.item_count)],
    ["Files / folders", `${formatCount(snapshot.file_count)} / ${formatCount(snapshot.folder_count)}`],
    ["Observed size", formatBytes(snapshot.total_size_bytes)],
  ];
  return (
    <section className="comparison-snapshot">
      <header>
        <h4>{label}</h4>
        {evidenceLink ? <Link to={evidenceLink}>Open access evidence</Link> : null}
      </header>
      <dl>
        {facts.map(([factLabel, value]) => (
          <div key={factLabel}>
            <dt>{factLabel}</dt>
            <dd title={value}>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ComparisonChangePanel({
  change,
  projectId,
  onClose,
}: {
  change: ResourceComparisonChange;
  projectId: string;
  onClose: () => void;
}) {
  const panelRef = useModalPanel<HTMLElement>(onClose, resourceChangeKey(change));

  return (
    <div className="access-evidence-layer" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside aria-labelledby="comparison-change-title" aria-modal="true" className="access-evidence-panel comparison-change-panel" ref={panelRef} role="dialog" tabIndex={-1}>
        <header className="access-evidence-panel-header">
          <div>
            <span>Resource change</span>
            <h2 id="comparison-change-title">{resourceChangeName(change)}</h2>
            <p>Before-and-after resource facts within the materialized comparison scope.</p>
          </div>
          <button aria-label="Close resource change details" onClick={onClose} type="button">Close</button>
        </header>
        <div className="access-evidence-panel-body">
          <section className="comparison-change-summary">
            <span className={`comparison-change-type is-${changeTypeTone(change.change_type)}`}>{changeTypeLabel(change.change_type)}</span>
            <div>{resourceChangeCategories(change).length > 0 ? resourceChangeCategories(change).map((category) => <span key={category}>{humanizeEvidenceValue(category)}</span>) : <span>No typed categories recorded</span>}</div>
          </section>

          {change.change_type === "indeterminate" ? (
            <StatusBanner tone="warning" title="Absence or change is indeterminate">
              <p>Collection scope or coverage was not authoritative enough to classify this as a confirmed appearance, disappearance, or change.</p>
            </StatusBanner>
          ) : null}

          <section aria-labelledby="comparison-dimensions-title" className="access-evidence-section">
            <div className="access-evidence-section-heading"><div><h3 id="comparison-dimensions-title">Comparison dimensions</h3><p>Each dimension is interpreted independently.</p></div></div>
            <dl className="access-evidence-inline-facts">
              <div><dt>Structure</dt><dd>{humanizeEvidenceValue(change.structural_state)}</dd></div>
              <div><dt>Access</dt><dd>{humanizeEvidenceValue(change.access_state)}</dd></div>
              <div><dt>Content</dt><dd>{humanizeEvidenceValue(change.content_state)}</dd></div>
            </dl>
            {change.access_interpretation ? <p className="access-evidence-empty-detail">{change.access_interpretation}</p> : null}
          </section>

          <div className="comparison-snapshot-grid">
            <SnapshotCard label="Baseline" projectId={projectId} snapshot={change.before} />
            <SnapshotCard label="Current" projectId={projectId} snapshot={change.after} />
          </div>

          <StatusBanner tone={change.item_changes?.state === "computed" ? (change.item_changes.exact ? "success" : "warning") : "info"} title={change.item_changes?.state === "computed" ? (change.item_changes.exact ? "Item-level comparison computed" : "Item-level comparison is bounded") : "Item-level comparison not computed"}>
            <p>{itemChangeCopy(change.item_changes)}</p>
            {change.item_changes?.state === "not_computed" ? <p className="mt-1">Null item counts mean not computed, never zero.</p> : null}
            {change.item_changes?.state === "computed" && !change.item_changes.exact ? <p className="mt-1">Counts describe materialized rows, but coverage or identity limitations prevent an exact absence/correlation conclusion.</p> : null}
          </StatusBanner>

          <section aria-labelledby="comparison-match-title" className="access-evidence-section">
            <div className="access-evidence-section-heading"><div><h3 id="comparison-match-title">Match provenance</h3><p>How the service associated the baseline and current resource.</p></div></div>
            <dl className="access-evidence-inline-facts">
              <div><dt>Basis</dt><dd>{humanizeEvidenceValue(change.match?.basis)}</dd></div>
              <div><dt>Quality</dt><dd>{humanizeEvidenceValue(change.match?.quality)}</dd></div>
              <div><dt>Provider</dt><dd>{humanizeEvidenceValue(resourceChangeProvider(change))}</dd></div>
            </dl>
          </section>
        </div>
      </aside>
    </div>
  );
}

export function ComparisonPage() {
  const { projectId, comparisonId } = useParams<{ projectId: string; comparisonId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [comparison, setComparison] = useState<ProjectComparison | null>(null);
  const [comparisonLoading, setComparisonLoading] = useState(true);
  const [comparisonRequestActive, setComparisonRequestActive] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [pollWarning, setPollWarning] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [pollNonce, setPollNonce] = useState(0);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);
  const [role, setRole] = useState<string | null>(null);
  const [roleReady, setRoleReady] = useState(false);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [comparisonRetryBusy, setComparisonRetryBusy] = useState(false);
  const [comparisonRetryError, setComparisonRetryError] = useState<string | null>(null);
  const [comparisonRetryInfo, setComparisonRetryInfo] = useState<string | null>(null);
  const [findingsRetryBusy, setFindingsRetryBusy] = useState(false);
  const [findingsRetryError, setFindingsRetryError] = useState<string | null>(null);
  const [findingsRetryInfo, setFindingsRetryInfo] = useState<string | null>(null);
  const [resultLevel, setResultLevel] = useState<"resources" | "items">(() => readSearchParam("level") === "items" ? "items" : "resources");

  const [changeType, setChangeTypeState] = useState<ResourceChangeType | "all">(() => normalizeChangeType(readSearchParam("changeType")));
  const [provider, setProviderState] = useState(() => readSearchParam("provider").toLowerCase());
  const [category, setCategoryState] = useState(() => readSearchParam("category").toLowerCase());
  const [query, setQueryState] = useState(() => readSearchParam("q"));
  const [selectedChangeKey, setSelectedChangeKey] = useState(() => readSearchParam("change"));
  const debouncedQuery = useDebouncedValue(query, 300);

  const [changes, setChanges] = useState<ResourceComparisonChange[]>([]);
  const [changesLoading, setChangesLoading] = useState(false);
  const [changesError, setChangesError] = useState<string | null>(null);
  const [changesReloadNonce, setChangesReloadNonce] = useState(0);
  const initialCursor = useMemo(() => readSearchParam("cursor") || null, []);
  const [cursor, setCursor] = useState<string | null>(initialCursor);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>(
    initialCursor ? [null] : [],
  );
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const filtersInitialized = useRef(false);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    for (const key of ["changeType", "provider", "category", "q", "cursor", "change", "level"]) next.delete(key);
    if (resultLevel === "items") next.set("level", "items");
    if (changeType !== "all") next.set("changeType", changeType);
    if (provider) next.set("provider", provider);
    if (category) next.set("category", category);
    if (query.trim()) next.set("q", query.trim());
    if (cursor) next.set("cursor", cursor);
    if (selectedChangeKey) next.set("change", selectedChangeKey);
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [category, changeType, cursor, provider, query, resultLevel, searchParams, selectedChangeKey, setSearchParams]);

  useEffect(() => {
    if (!projectId || !comparisonId) {
      setComparisonLoading(false);
      setComparisonError("The project or comparison identifier is missing from this route.");
      return;
    }
    const controller = new AbortController();
    setComparisonRequestActive(true);
    if (!comparison) setComparisonLoading(true);
    setComparisonError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/comparisons/${encodeURIComponent(comparisonId)}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const payload = data as ProjectComparison;
        if (!payload?.id || payload.id.toLowerCase() !== comparisonId.toLowerCase()) {
          setComparisonError("The comparison response did not match the comparison requested by this route.");
          return;
        }
        setComparison(payload);
        if (payload.state !== "failed") setComparisonRetryError(null);
        setPollWarning(null);
        setLastUpdatedAt(new Date());
      })
      .catch((caught) => {
        if (controller.signal.aborted || isAbortError(caught)) return;
        const message = caught instanceof Error ? caught.message : "Comparison could not be loaded.";
        if (comparison) setPollWarning(`${message} The last successful state remains visible.`);
        else setComparisonError(message);
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setComparisonLoading(false);
          setComparisonRequestActive(false);
        }
      });
    return () => controller.abort();
  }, [comparisonId, pollNonce, projectId, reloadNonce]);

  useEffect(() => {
    const evaluation = comparisonFindingsEvaluation(comparison);
    if (
      !projectId
      || (comparison?.state !== "failed" && monitoringEvaluationState(evaluation) !== "degraded")
    ) {
      setRole(null);
      setRoleReady(false);
      setRoleError(null);
      return;
    }
    const controller = new AbortController();
    setRoleReady(false);
    setRoleError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/my-role`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setRole(typeof data?.role === "string" ? data.role.toLowerCase() : null);
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) {
          setRole(null);
          setRoleError(caught instanceof Error ? caught.message : "Recovery permission could not be verified.");
        }
      })
      .finally(() => !controller.signal.aborted && setRoleReady(true));
    return () => controller.abort();
  }, [comparison?.id, comparison?.state, comparison?.summary?.findings_evaluation?.state, projectId]);

  useEffect(() => {
    if (
      comparisonRequestActive
      || (
        comparison?.state !== "queued"
        && comparison?.state !== "running"
        && !monitoringEvaluationIsActive(comparisonFindingsEvaluation(comparison))
      )
    ) return;
    const timer = window.setTimeout(
      () => setPollNonce((value) => value + 1),
      pollWarning ? 5000 : 2500,
    );
    return () => window.clearTimeout(timer);
  }, [comparison?.state, comparison?.summary?.findings_evaluation?.state, comparisonRequestActive, pollNonce, pollWarning]);

  useEffect(() => {
    if (!filtersInitialized.current) {
      filtersInitialized.current = true;
      return;
    }
    setCursor(null);
    setCursorHistory([]);
    setNextCursor(null);
  }, [category, changeType, debouncedQuery, provider]);

  useEffect(() => {
    if (!projectId || !comparisonId || comparison?.state !== "complete" || resultLevel !== "resources") {
      setChanges([]);
      setNextCursor(null);
      setChangesLoading(false);
      return;
    }
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (changeType !== "all") params.set("change_type", changeType);
    if (provider) params.set("provider", provider);
    if (category) params.set("category", category);
    if (debouncedQuery.trim()) params.set("q", debouncedQuery.trim());
    if (cursor) params.set("cursor", cursor);
    setChangesLoading(true);
    setChangesError(null);
    setChanges([]);
    setNextCursor(null);
    apiFetch(
      `/projects/${encodeURIComponent(projectId)}/comparisons/${encodeURIComponent(comparisonId)}/resource-changes?${params.toString()}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (controller.signal.aborted) return;
        const page = (data || {}) as Partial<ResourceChangePage>;
        setChanges(Array.isArray(page.items) ? page.items.map((change) => ({
          ...change,
          change_categories: resourceChangeCategories(change),
          before: change.before ? {
            ...change.before,
            run_id: change.before.run_id || comparisonRunId(comparison, "baseline"),
            provider: change.before.provider || change.provider,
            resource_type: change.before.resource_type || change.resource_type,
            provider_resource_id: change.before.provider_resource_id || change.provider_resource_id,
          } : null,
          after: change.after ? {
            ...change.after,
            run_id: change.after.run_id || comparisonRunId(comparison, "current"),
            provider: change.after.provider || change.provider,
            resource_type: change.after.resource_type || change.resource_type,
            provider_resource_id: change.after.provider_resource_id || change.provider_resource_id,
          } : null,
        })) : []);
        setNextCursor(page.next_cursor || null);
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) {
          setChangesError(caught instanceof Error ? caught.message : "Resource changes could not be loaded.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setChangesLoading(false);
      });
    return () => controller.abort();
  }, [category, changeType, changesReloadNonce, comparison?.state, comparisonId, cursor, debouncedQuery, projectId, provider, resultLevel]);

  const selectedChange = useMemo(
    () => changes.find((change, index) => resourceChangeKey(change, index) === selectedChangeKey) || null,
    [changes, selectedChangeKey],
  );
  const summary = comparison?.summary;
  const summaryCounts = comparisonSummaryCounts(summary);
  const countFilters: Array<{ key: ResourceChangeType | "all"; label: string; count: number }> = [
    { key: "all", label: "All changes", count: summaryCounts.total },
    { key: "appeared", label: "Appeared", count: summaryCounts.appeared },
    { key: "disappeared", label: "Disappeared", count: summaryCounts.disappeared },
    { key: "changed", label: "Changed", count: summaryCounts.changed },
    { key: "indeterminate", label: "Indeterminate", count: summaryCounts.indeterminate },
  ];
  const currentRunId = comparison ? comparisonRunId(comparison, "current") : null;
  const baselineRunId = comparison ? comparisonRunId(comparison, "baseline") : null;
  const boundedPreviewLink = currentRunId
    ? `/projects/${projectId}/runs/${currentRunId}?${new URLSearchParams({
        view: "diff",
        ...(baselineRunId ? { baselineRun: baselineRunId } : {}),
      }).toString()}`
    : null;
  const compatibility = comparison?.compatibility;
  const capabilitySatisfied = compatibility?.capability_applicable === false
    || compatibility?.capability_interpretable === true;
  const identitySatisfied = compatibility?.identity_applicable === false
    || compatibility?.identity_scope_exact !== false;
  const comparisonActive = comparison?.state === "queued" || comparison?.state === "running";
  const findingsEvaluation = comparisonFindingsEvaluation(comparison);
  const findingsState = monitoringEvaluationState(findingsEvaluation);
  const findingsEvaluationActive = monitoringEvaluationIsActive(findingsEvaluation);
  const findingsAttemptCount = safeMonitoringCount(findingsEvaluation?.attempt_count);
  const findingsErrorCode = safeMonitoringDiagnostic(findingsEvaluation?.error_code);

  async function retryComparisonFindings() {
    if (!projectId || !comparisonId || !canRetryComparisonFindings(comparison, role)) return;
    setFindingsRetryBusy(true);
    setFindingsRetryError(null);
    setFindingsRetryInfo(null);
    try {
      const response = await apiFetch(
        `/projects/${encodeURIComponent(projectId)}/comparisons/${encodeURIComponent(comparisonId)}/findings/retry`,
        { method: "POST" },
      );
      const nextEvaluation = response?.findings_evaluation;
      if (nextEvaluation && typeof nextEvaluation === "object") {
        setComparison((current) => current ? {
          ...current,
          summary: { ...(current.summary || {}), findings_evaluation: nextEvaluation },
        } : current);
      }
      setFindingsRetryInfo("Finding evaluation recovery was queued. Materialized change results remain available while the derived workflow runs.");
    } catch (caught) {
      setFindingsRetryError(caught instanceof Error ? caught.message : "Finding evaluation recovery could not be requested.");
    } finally {
      setFindingsRetryBusy(false);
      setPollNonce((value) => value + 1);
    }
  }

  async function retryMaterializedComparison() {
    if (!projectId || !comparisonId || !canRetryMaterializedComparison(comparison, role)) return;
    setComparisonRetryBusy(true);
    setComparisonRetryError(null);
    setComparisonRetryInfo(null);
    try {
      const response = await apiFetch(
        `/projects/${encodeURIComponent(projectId)}/comparisons/${encodeURIComponent(comparisonId)}/retry`,
        { method: "POST" },
      );
      const payload = response as ProjectComparison;
      if (!payload?.id || payload.id.toLowerCase() !== comparisonId.toLowerCase()) {
        throw new Error("The retry response did not match this comparison. Reload before trying again.");
      }
      setComparison(payload);
      setComparisonRetryInfo("Comparison recovery was queued. This page will refresh as materialization resumes from a clean retry state.");
    } catch (caught) {
      setComparisonRetryError(caught instanceof Error ? caught.message : "Comparison recovery could not be requested.");
    } finally {
      setComparisonRetryBusy(false);
      setPollNonce((value) => value + 1);
    }
  }

  function changeFilter(next: ResourceChangeType | "all") {
    setChangeTypeState(next);
    setSelectedChangeKey("");
  }

  function changeProvider(next: string) {
    setProviderState(next);
    setSelectedChangeKey("");
  }

  function changeCategory(next: string) {
    setCategoryState(next);
    setSelectedChangeKey("");
  }

  function changeQuery(next: string) {
    setQueryState(next);
    setSelectedChangeKey("");
  }

  function moveNext() {
    if (!nextCursor) return;
    setCursorHistory((history) => [...history, cursor]);
    setCursor(nextCursor);
    setSelectedChangeKey("");
  }

  function movePrevious() {
    setCursorHistory((history) => {
      if (history.length === 0) return history;
      setCursor(history[history.length - 1]);
      return history.slice(0, -1);
    });
    setSelectedChangeKey("");
  }

  if (!comparison && (comparisonLoading || comparisonError)) {
    return (
      <section className="workspace">
        <header className="workspace-header"><div className="workspace-card"><p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Resource comparison</p><h1 className="mt-1 text-2xl font-semibold">{comparisonLoading ? "Loading comparison" : "Comparison unavailable"}</h1><p className="mt-2 break-all text-sm text-slate-500">Comparison ID: {comparisonId || "Not available"}</p></div></header>
        <div className="workspace-section">
          <StatePanel
            actions={comparisonError ? <button className="inventory-button-primary" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry comparison</button> : null}
            description={comparisonError || "Loading comparison state and run context."}
            title={comparisonError ? "Comparison could not be loaded" : "Loading comparison"}
            tone={comparisonError ? "error" : "neutral"}
          />
        </div>
      </section>
    );
  }

  if (!comparison) return null;

  return (
    <section className="comparison-workspace">
      <header className="comparison-page-header">
        <div>
          <nav aria-label="Breadcrumb">
            <Link to="/projects">Projects</Link><span aria-hidden="true">/</span>
            <Link to={`/projects/${projectId}/changes`}>Changes</Link><span aria-hidden="true">/</span>
            <span>Comparison</span>
          </nav>
          <div className="comparison-title-row">
            <div>
              <p>Resource comparison</p>
              <h1>{comparisonRunLabel(comparison, "baseline")} <span aria-hidden="true">→</span> {comparisonRunLabel(comparison, "current")}</h1>
            </div>
            <span className={`comparison-state is-${comparisonStateTone(comparison.state)}`}>{humanizeEvidenceValue(comparison.state)}</span>
          </div>
          <p className="comparison-page-subtitle">Server-materialized resource and item history with dimension-specific comparability. The bounded run preview remains available for legacy comparisons.</p>
        </div>
        <div className="comparison-header-actions">
          {boundedPreviewLink ? <Link className="inventory-button-primary" to={boundedPreviewLink}>Open bounded item preview</Link> : null}
          <Link className="inventory-button-secondary" to={`/projects/${projectId}/inventory`}>Open inventory</Link>
        </div>
      </header>

      {pollWarning ? (
        <StatusBanner tone="warning" title="Live comparison state may be stale"><p>{pollWarning}</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setPollNonce((value) => value + 1)} type="button">Retry status</button></StatusBanner>
      ) : null}

      {comparisonRetryError ? <StatusBanner tone="error" title="Comparison recovery request failed"><p>{comparisonRetryError} The authoritative comparison state is being reloaded before another attempt.</p></StatusBanner> : null}
      {comparisonRetryInfo ? <StatusBanner tone="success" title="Comparison recovery requested"><p>{comparisonRetryInfo}</p></StatusBanner> : null}

      {findingsRetryError ? <StatusBanner tone="error" title="Finding recovery request failed"><p>{findingsRetryError} Comparison state is being reloaded before another attempt.</p></StatusBanner> : null}
      {findingsRetryInfo ? <StatusBanner tone="success" title="Finding recovery requested"><p>{findingsRetryInfo}</p></StatusBanner> : null}

      {comparison.state === "complete" && !findingsEvaluation ? (
        <StatusBanner tone="warning" title="Finding evaluation state is not recorded">
          <p>This comparison may predate continuous finding evaluation. Change evidence remains available, but do not infer that policies were evaluated.</p>
        </StatusBanner>
      ) : null}

      {comparison.state === "complete" && findingsEvaluation ? (
        <StatusBanner
          tone={findingsState === "complete" ? "success" : findingsState === "degraded" ? "error" : "warning"}
          title={findingsState === "complete" ? "Finding evaluation complete" : findingsState === "degraded" ? "Finding evaluation is degraded" : findingsEvaluationActive ? "Finding evaluation is in progress" : "Finding evaluation state is indeterminate"}
        >
          <p>The materialized comparison remains valid. This status describes the separate policy-evaluation workflow.</p>
          <dl className="monitoring-evidence-facts">
            <div><dt>State</dt><dd>{humanizeEvidenceValue(findingsState)}</dd></div>
            <div><dt>Attempts</dt><dd>{findingsAttemptCount == null ? "Not recorded" : findingsAttemptCount.toLocaleString()}</dd></div>
            <div><dt>Next retry</dt><dd>{formatMonitoringTimestamp(findingsEvaluation.next_retry_at)}</dd></div>
            <div><dt>Authority</dt><dd>{findingsEvaluation.authoritative_state === true ? "Authoritative current baseline" : findingsEvaluation.authoritative_state === false ? "Positive evidence only" : "Not recorded"}</dd></div>
            <div><dt>Partial evidence retained</dt><dd>{findingsEvaluation.partial_positive_evidence_retained === true ? "Yes" : findingsEvaluation.partial_positive_evidence_retained === false ? "No" : "Not recorded"}</dd></div>
            <div><dt>Safe diagnostic</dt><dd>{findingsErrorCode || "None recorded"}</dd></div>
          </dl>
          {findingsState === "degraded" ? <p className="mt-2">New positive evidence may have been retained, but absence-based conclusions remain bounded until evaluation completes.</p> : null}
          {canRetryComparisonFindings(comparison, role) ? <button className="inventory-button-primary mt-2" disabled={findingsRetryBusy} onClick={() => void retryComparisonFindings()} type="button">{findingsRetryBusy ? "Requesting retry…" : "Retry finding evaluation"}</button> : null}
          {findingsState === "degraded" && roleError ? <p className="mt-2">{roleError} Recovery remains disabled until access can be verified.</p> : null}
          {findingsState === "degraded" && !roleReady && !roleError ? <p className="mt-2">Checking recovery permission…</p> : null}
        </StatusBanner>
      ) : null}

      <section className="comparison-run-context">
        <div><span>Baseline</span><strong>{comparisonRunLabel(comparison, "baseline")}</strong><small>{runTimestamp(comparison.baseline_run) || baselineRunId || "Timestamp not recorded"}</small></div>
        <div><span>Current</span><strong>{comparisonRunLabel(comparison, "current")}</strong><small>{runTimestamp(comparison.current_run) || currentRunId || "Timestamp not recorded"}</small></div>
        <div><span>State</span><strong>{humanizeEvidenceValue(comparison.state)}</strong><small>{comparisonStateCopy(comparison)}</small></div>
        <div><span>Freshness</span><strong>{lastUpdatedAt ? lastUpdatedAt.toLocaleTimeString() : "Not loaded"}</strong><small>{comparisonActive || findingsEvaluationActive ? "Polling every few seconds" : "Terminal state"}</small></div>
      </section>

      {comparisonActive ? (
        <StatePanel description={comparisonStateCopy(comparison)} title={comparison.state === "queued" ? "Comparison queued" : "Comparison running"} />
      ) : null}

      {comparison.state === "failed" ? (
        <StatusBanner tone="error" title="Comparison failed">
          <p>{comparisonStateCopy(comparison)}</p>
          <p className="mt-1">No partial result rows are published as final. After correcting the recorded failure, retry this comparison so its durable identity and audit history are preserved.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {canRetryMaterializedComparison(comparison, role) ? <button className="inventory-button-primary" disabled={comparisonRetryBusy} onClick={() => void retryMaterializedComparison()} type="button">{comparisonRetryBusy ? "Requesting retry…" : "Retry comparison"}</button> : null}
            {boundedPreviewLink ? <Link className="inline-flex rounded-md border border-current px-3 py-2 text-xs font-semibold" to={boundedPreviewLink}>Return to run diff</Link> : null}
          </div>
          {roleError ? <p className="mt-2">{roleError} Recovery remains disabled until access can be verified.</p> : null}
          {!roleReady && !roleError ? <p className="mt-2">Checking recovery permission…</p> : null}
          {roleReady && !canRetryMaterializedComparison(comparison, role) && !roleError ? <p className="mt-2">An operator or project administrator must retry this comparison.</p> : null}
        </StatusBanner>
      ) : null}

      {comparison.state === "complete" ? (
        <>
          <StatusBanner tone={comparisonCompatibilityTone(compatibility)} title={compatibility?.structural_interpretable ? (compatibility.content_interpretable && compatibility.access_interpretable && identitySatisfied && capabilitySatisfied && compatibility.direct_permissions_interpretable ? "Comparison dimensions are interpretable" : "Some comparison dimensions are limited") : "Structural changes are not fully interpretable"}>
            <div className="comparison-compatibility-grid">
              <span><strong>Structure</strong>{compatibility?.structural_interpretable ? "Interpretable" : "Indeterminate"}</span>
              <span><strong>Item-count signal</strong>{compatibility?.content_interpretable ? "Comparable" : "Not comparable"}</span>
              <span><strong>Identity continuity</strong>{compatibility?.identity_applicable === false ? "Not applicable" : compatibility?.identity_scope_exact ? "Strong server identity" : "Location-bound"}</span>
              <span><strong>Access context</strong>{compatibility?.access_context_comparable ? "Comparable" : "Not comparable"}</span>
              <span><strong>Capability observations</strong>{compatibility?.capability_applicable === false ? "Not applicable" : compatibility?.capability_interpretable ? "Comparable" : "Not comparable"}</span>
              <span><strong>Provider permission evidence</strong>{compatibility?.direct_permissions_interpretable ? (compatibility.direct_permissions_scope_exact ? "Scope-complete and comparable" : "Comparable, bounded scope") : compatibility?.direct_permissions_assessed ? "Incomplete" : "Not assessed"}</span>
            </div>
            {(compatibility?.reasons || []).length > 0 ? <ul className="mt-2 list-disc space-y-1 pl-5">{compatibility?.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
          </StatusBanner>

          <StatusBanner tone="info" title="Dimension-specific change evidence">
            <p>Resource summary counts are {summary?.resource_summary_exact ? "exact for the published comparable scope" : "not marked exact; review coverage before acting"}. Item history separately reports whether it was computed and exact.</p>
            <p className="mt-1">“Not computed” is distinct from zero change. The bounded run preview remains available for legacy comparisons that did not materialize item rows.</p>
          </StatusBanner>

          <nav aria-label="Comparison result level" className="comparison-level-tabs">
            <button aria-current={resultLevel === "resources" ? "page" : undefined} className={resultLevel === "resources" ? "is-active" : ""} onClick={() => setResultLevel("resources")} type="button">Resource changes</button>
            <button aria-current={resultLevel === "items" ? "page" : undefined} className={resultLevel === "items" ? "is-active" : ""} onClick={() => { setResultLevel("items"); setSelectedChangeKey(""); }} type="button">Item history</button>
          </nav>

          {resultLevel === "resources" ? <>
            <section aria-label="Resource change filters" className="comparison-filter-bar">
            <div aria-label="Filter by change type" className="comparison-count-filters" role="group">
              {countFilters.map((filter) => (
                <button aria-pressed={changeType === filter.key} className={changeType === filter.key ? "is-active" : ""} key={filter.key} onClick={() => changeFilter(filter.key)} type="button">
                  <span>{filter.label}</span><strong>{filter.count.toLocaleString()}</strong>
                </button>
              ))}
            </div>
            <div className="comparison-query-filters">
              <label>Search resources<input onChange={(event) => changeQuery(event.target.value)} placeholder="Name, endpoint, or provider ID" type="search" value={query} /></label>
              <label>Provider<select onChange={(event) => changeProvider(event.target.value)} value={provider}><option value="">All providers</option><option value="smb">SMB</option><option value="nfs">NFS</option><option value="sharepoint">SharePoint</option></select></label>
              <label>Evidence category<select onChange={(event) => changeCategory(event.target.value)} value={category}><option value="">All categories</option>{COMPARISON_CATEGORY_OPTIONS.map((option) => <option key={option} value={option}>{humanizeEvidenceValue(option)}</option>)}</select></label>
            </div>
            </section>

            <section aria-busy={changesLoading} aria-labelledby="resource-changes-title" className="comparison-results">
            <header><div><h2 id="resource-changes-title">Resource changes</h2><p>Server-filtered and cursor-paginated. Inspect a row without losing filter or page context.</p></div><span>Page {cursorHistory.length + 1} · up to {PAGE_LIMIT} rows</span></header>
            {changesError ? <StatusBanner tone="error" title="Resource changes unavailable"><p>{changesError}</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setChangesReloadNonce((value) => value + 1)} type="button">Retry changes</button></StatusBanner> : null}
            {!changesLoading && !changesError && selectedChangeKey && !selectedChange ? (
              <StatusBanner tone="warning" title="Linked change is not on this result page">
                <p>The saved change identifier is stale or excluded by the current filters. The visible page remains unchanged.</p>
                <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setSelectedChangeKey("")} type="button">Clear linked change</button>
              </StatusBanner>
            ) : null}
            {changesLoading ? <div className="inventory-skeleton" aria-label="Loading resource changes" role="status">{Array.from({ length: 8 }, (_, index) => <span key={index} />)}</div> : null}
            {!changesLoading && !changesError && changes.length === 0 ? (
              <StatePanel description={emptyResourceChangesDescription(summary, changeType !== "all" || Boolean(provider || category || debouncedQuery))} title="No resource changes" />
            ) : null}
            {!changesLoading && changes.length > 0 ? (
              <div className="comparison-table-scroll">
                <table className="comparison-table">
                  <caption className="sr-only">Materialized resource changes between the baseline and current run</caption>
                  <thead><tr><th scope="col">Change</th><th scope="col">Resource</th><th scope="col">Provider</th><th scope="col">Categories</th><th scope="col">Access evidence</th><th scope="col">Dimension states</th><th scope="col">Match</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>
                    {changes.map((change, index) => {
                      const key = resourceChangeKey(change, index);
                      const snapshot = changeSnapshot(change);
                      const beforeAccess = snapshotEvidenceLabel(change.before);
                      const afterAccess = snapshotEvidenceLabel(change.after);
                      return (
                        <tr className={selectedChangeKey === key ? "is-selected" : undefined} key={key}>
                          <td><span className={`comparison-change-type is-${changeTypeTone(change.change_type)}`}>{changeTypeLabel(change.change_type)}</span></td>
                          <td><button className="comparison-resource-button" onClick={() => setSelectedChangeKey(key)} type="button"><strong>{resourceChangeName(change)}</strong><small>{snapshot?.hostname || snapshot?.endpoint_key || "Endpoint not recorded"}</small></button></td>
                          <td><ProviderBadge provider={resourceChangeProvider(change)} /></td>
                          <td><div className="comparison-category-list">{resourceChangeCategories(change).length > 0 ? resourceChangeCategories(change).map((rowCategory) => <button aria-label={`Filter by ${humanizeEvidenceValue(rowCategory)} category`} className={category === rowCategory ? "is-active" : undefined} key={rowCategory} onClick={() => changeCategory(rowCategory)} type="button">{humanizeEvidenceValue(rowCategory)}</button>) : <span>None recorded</span>}</div></td>
                          <td><div className="comparison-before-after"><span><small>Baseline</small>{beforeAccess}</span><span aria-hidden="true">→</span><span><small>Current</small>{afterAccess}</span>{change.access_interpretation ? <p>{change.access_interpretation}</p> : null}</div></td>
                          <td><div className="comparison-dimension-list"><span>Structure: {humanizeEvidenceValue(change.structural_state)}</span><span>Access: {humanizeEvidenceValue(change.access_state)}</span><span>Content: {humanizeEvidenceValue(change.content_state)}</span><small>{itemChangeCopy(change.item_changes)}</small></div></td>
                          <td><div className="comparison-match"><strong>{humanizeEvidenceValue(change.match?.quality)}</strong><small>{humanizeEvidenceValue(change.match?.basis)}</small></div></td>
                          <td><button aria-label={`Inspect change for ${resourceChangeName(change)}`} className="comparison-inspect-button" onClick={() => setSelectedChangeKey(key)} type="button">Inspect</button></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}
            <footer className="inventory-pagination"><span>Opaque cursor pagination preserves stable server order.</span><nav aria-label="Resource change pages" className="inventory-page-controls"><button disabled={cursorHistory.length === 0 || changesLoading} onClick={movePrevious} type="button">Previous</button><span aria-current="page" className="px-2 font-semibold">Page {cursorHistory.length + 1}</span><button disabled={!nextCursor || changesLoading} onClick={moveNext} type="button">Next</button></nav></footer>
            </section>
          </> : projectId && comparisonId ? <ComparisonItemChanges comparisonId={comparisonId} projectId={projectId} /> : null}
        </>
      ) : null}

      {resultLevel === "resources" && selectedChange && projectId ? <ComparisonChangePanel change={selectedChange} onClose={() => setSelectedChangeKey("")} projectId={projectId} /> : null}
    </section>
  );
}
