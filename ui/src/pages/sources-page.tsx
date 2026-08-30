import { FormEvent, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  automaticBaselineIsActive,
  buildSourceUpdatePayload,
  canManageSources,
  canRetrySourceMonitoring,
  formatDuration,
  formatMonitoringTimestamp,
  humanizeMonitoringValue,
  monitoringEvaluationIsActive,
  monitoringEvaluationState,
  safeMonitoringCount,
  safeMonitoringDiagnostic,
  sourceUpdateHasChanges,
  type MonitoringSource,
  type SourceHealth,
} from "@/lib/monitoring";

const PAGE_LIMIT = 50;
const SOURCE_HEALTH: SourceHealth[] = ["healthy", "stale", "degraded", "never_collected", "disabled"];
const PROVIDERS = [
  { value: "smb", label: "SMB (including mixed network scans)" },
  { value: "sharepoint", label: "SharePoint" },
  { value: "nfs", label: "NFS (including mixed network scans)" },
  { value: "nfs+smb", label: "NFS + SMB only" },
];

function readParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function normalizeHealth(value: string): SourceHealth | "" {
  return SOURCE_HEALTH.includes(value as SourceHealth) ? value as SourceHealth : "";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function SourceDetail({ source, loading, error, canManage, canRetryFindings, permissionsReady, permissionError, busy, onReload, onRetryFindings, onUpdate }: {
  source: MonitoringSource | null;
  loading: boolean;
  error: string | null;
  canManage: boolean;
  canRetryFindings: boolean;
  permissionsReady: boolean;
  permissionError: string | null;
  busy: boolean;
  onReload: () => void;
  onRetryFindings: () => Promise<void>;
  onUpdate: (payload: Record<string, unknown>) => Promise<MonitoringSource | null>;
}) {
  const [displayName, setDisplayName] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [interval, setInterval] = useState("");
  const [draftBase, setDraftBase] = useState<MonitoringSource | null>(null);
  const [serverConfigurationChanged, setServerConfigurationChanged] = useState(false);

  function loadSourceDraft(value: MonitoringSource) {
    setDisplayName(value.display_name || "");
    setEnabled(value.enabled);
    setInterval(value.expected_interval_seconds == null ? "" : String(value.expected_interval_seconds));
    setDraftBase(value);
    setServerConfigurationChanged(false);
  }

  useEffect(() => {
    if (!source) return;
    if (!draftBase || draftBase.id !== source.id) {
      loadSourceDraft(source);
      return;
    }
    const draftInterval = interval.trim() === "" ? null : Number(interval);
    const draftDirty = sourceUpdateHasChanges(buildSourceUpdatePayload(draftBase, {
      displayName,
      enabled,
      expectedIntervalSeconds: draftInterval === null || Number.isSafeInteger(draftInterval)
        ? draftInterval
        : draftBase.expected_interval_seconds ?? null,
    }));
    const configurationChanged = source.display_name !== draftBase.display_name
      || source.enabled !== draftBase.enabled
      || (source.expected_interval_seconds ?? null) !== (draftBase.expected_interval_seconds ?? null);
    if (!draftDirty) {
      loadSourceDraft(source);
    } else if (configurationChanged) {
      setServerConfigurationChanged(true);
    } else if (source.updated_at !== draftBase.updated_at) {
      // Monitoring progress also advances updated_at. Preserve the operator's
      // draft while refreshing its unchanged configuration baseline.
      setDraftBase({ ...draftBase, updated_at: source.updated_at });
    }
  }, [source?.id, source?.updated_at]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!source || serverConfigurationChanged) return;
    const parsedInterval = interval.trim() === "" ? null : Number(interval);
    if (!displayName.trim() || (parsedInterval !== null && (!Number.isSafeInteger(parsedInterval) || parsedInterval < 300 || parsedInterval > 31_536_000))) return;
    const payload = buildSourceUpdatePayload(draftBase || source, {
      displayName,
      enabled,
      expectedIntervalSeconds: parsedInterval,
    });
    if (!sourceUpdateHasChanges(payload)) return;
    const updated = await onUpdate(payload);
    if (updated) loadSourceDraft(updated);
  }

  if (loading) return <StatePanel description="Loading source scope, cadence, and collection health." title="Loading source" />;
  if (error) return <StatePanel actions={<button className="inventory-button-primary" onClick={onReload} type="button">Retry source</button>} description={`${error} Retrying this read does not trigger collection.`} title="Source unavailable" tone="error" />;
  if (!source) return <StatePanel description="Choose a source to inspect collection scope, coverage, freshness, and configuration." title="Select a source" />;

  const coverageReasons = source.coverage?.reasons || [];
  const healthReasons = source.health_reasons || [];
  const findingsEvaluation = source.coverage?.monitoring_findings;
  const findingsState = monitoringEvaluationState(findingsEvaluation);
  const findingsActive = monitoringEvaluationIsActive(findingsEvaluation);
  const findingsAttemptCount = safeMonitoringCount(findingsEvaluation?.attempt_count);
  const findingsObserved = safeMonitoringCount(findingsEvaluation?.observed);
  const findingsResolved = safeMonitoringCount(findingsEvaluation?.resolved);
  const findingsErrorCode = safeMonitoringDiagnostic(findingsEvaluation?.error_code);
  const findingsReason = safeMonitoringDiagnostic(findingsEvaluation?.reason);
  const baseline = source.coverage?.automatic_baseline;
  const baselineState = safeMonitoringDiagnostic(baseline?.state);
  const baselineFindingsState = safeMonitoringDiagnostic(baseline?.findings_evaluation_state);
  const baselineErrorCode = safeMonitoringDiagnostic(baseline?.error_code);
  const nameInvalid = !displayName.trim();
  const intervalInvalid = interval.trim() !== "" && (!Number.isSafeInteger(Number(interval)) || Number(interval) < 300 || Number(interval) > 31_536_000);
  const parsedInterval = interval.trim() === "" ? null : Number(interval);
  const pendingPayload = buildSourceUpdatePayload(draftBase || source, {
    displayName,
    enabled,
    expectedIntervalSeconds: intervalInvalid ? source.expected_interval_seconds ?? null : parsedInterval,
  });
  const updateDirty = sourceUpdateHasChanges(pendingPayload);
  return (
    <article className="monitoring-detail-card">
      <header className="monitoring-detail-header">
        <div><div className="monitoring-badges"><span className={`source-health is-${source.health_status}`}>{humanizeMonitoringValue(source.health_status)}</span><ProviderBadge provider={source.provider} /></div><h2>{source.display_name}</h2><p>Automatically registered from normalized run context. Share Sentinel stores scope and health metadata, not collector credentials.</p></div>
      </header>

      {(source.health_status !== "healthy" || source.coverage?.state !== "complete") ? <StatusBanner tone={source.health_status === "degraded" ? "error" : "warning"} title="Collection confidence requires review"><p>{[...healthReasons, ...coverageReasons].join(" ") || "The source is stale, disabled, or has incomplete collection coverage."}</p></StatusBanner> : null}

      <dl className="monitoring-fact-grid">
        <div><dt>Provider</dt><dd>{humanizeMonitoringValue(source.provider)}</dd></div>
        <div><dt>Assessed identity</dt><dd>{source.assessed_identity || "Not recorded"}</dd></div>
        <div><dt>Coverage</dt><dd>{humanizeMonitoringValue(source.coverage?.state)}</dd></div>
        <div><dt>Freshness</dt><dd>{humanizeMonitoringValue(source.freshness?.state)} · age {formatDuration(source.freshness?.age_seconds)}</dd></div>
        <div><dt>Expected cadence</dt><dd>{source.expected_interval_seconds == null ? "Not configured" : `Every ${formatDuration(source.expected_interval_seconds)}`}</dd></div>
        <div><dt>Collector version</dt><dd>{source.collector_version || "Not recorded"}</dd></div>
        <div><dt>Last success</dt><dd>{formatMonitoringTimestamp(source.last_success_at)}</dd></div>
        <div><dt>Last failure</dt><dd>{formatMonitoringTimestamp(source.last_failure_at)}</dd></div>
        <div><dt>Stable source key</dt><dd className="font-mono" title={source.source_key}>{source.source_key}</dd></div>
        <div><dt>Source ID</dt><dd className="font-mono" title={source.id}>{source.id}</dd></div>
      </dl>

      <section aria-labelledby="source-evaluation-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading">
          <div>
            <h3 id="source-evaluation-title">Automated security evaluation</h3>
            <p>Inventory ingestion and derived finding evaluation are separate. A complete run can remain usable while this derived phase recovers.</p>
          </div>
          <span className={`evidence-state is-${findingsState === "complete" ? "exact" : findingsState === "degraded" ? "indeterminate" : "bounded"}`}>{humanizeMonitoringValue(findingsState)}</span>
        </div>
        <dl className="monitoring-evidence-facts">
          <div><dt>Finding evaluation</dt><dd>{humanizeMonitoringValue(findingsState)}</dd></div>
          <div><dt>Phase</dt><dd>{humanizeMonitoringValue(safeMonitoringDiagnostic(findingsEvaluation?.phase))}</dd></div>
          <div><dt>Attempts</dt><dd>{findingsAttemptCount == null ? "Not recorded" : findingsAttemptCount.toLocaleString()}</dd></div>
          <div><dt>Next retry</dt><dd>{formatMonitoringTimestamp(findingsEvaluation?.next_retry_at)}</dd></div>
          <div><dt>Findings observed</dt><dd>{findingsObserved == null ? "Not recorded" : findingsObserved.toLocaleString()}</dd></div>
          <div><dt>Findings resolved</dt><dd>{findingsResolved == null ? "Not recorded" : findingsResolved.toLocaleString()}</dd></div>
          <div><dt>Automatic baseline</dt><dd>{humanizeMonitoringValue(baselineState)}</dd></div>
          <div><dt>Baseline finding evaluation</dt><dd>{humanizeMonitoringValue(baselineFindingsState)}</dd></div>
          <div><dt>Baseline next retry</dt><dd>{formatMonitoringTimestamp(baseline?.findings_next_retry_at || baseline?.next_retry_at)}</dd></div>
          <div><dt>Baseline diagnostic</dt><dd>{baselineErrorCode || "None recorded"}</dd></div>
        </dl>
        {findingsErrorCode || findingsReason ? <p className="monitoring-trust-copy">Last safe diagnostic: <code>{findingsErrorCode || findingsReason}</code>. Raw exception text is not exposed here.</p> : null}
        {findingsState === "degraded" ? <StatusBanner tone="error" title="Finding evaluation needs operator recovery"><p>The latest inventory remains available, but finding coverage is partial until this phase completes.</p>{canRetryFindings ? <button className="inventory-button-primary mt-2" disabled={busy} onClick={() => void onRetryFindings()} type="button">{busy ? "Working…" : "Retry finding evaluation"}</button> : null}{permissionError ? <p className="mt-2">{permissionError} Recovery remains disabled until access can be verified.</p> : !permissionsReady ? <p className="mt-2">Checking recovery permission…</p> : null}</StatusBanner> : null}
        {findingsActive ? <StatusBanner tone="warning" title="Finding evaluation is in progress"><p>This view refreshes automatically. Attempts {findingsAttemptCount == null ? "are not yet recorded" : findingsAttemptCount.toLocaleString()}; next scheduled retry {formatMonitoringTimestamp(findingsEvaluation?.next_retry_at)}.</p></StatusBanner> : null}
        {baseline?.comparison_id ? <div className="monitoring-action-row"><Link className="inventory-button-secondary" to={`/projects/${source.project_id}/comparisons/${encodeURIComponent(baseline.comparison_id)}`}>Inspect automatic comparison</Link></div> : null}
      </section>

      <section aria-labelledby="source-actions-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-actions-title">Investigation paths</h3><p>Use source-scoped history before treating a missing resource as a confirmed disappearance.</p></div></div>
        <div className="monitoring-action-row">
          {source.last_run_id ? <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/runs/${source.last_run_id}`}>Open latest run</Link> : null}
          <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/findings?${new URLSearchParams({ source: source.id }).toString()}`}>Open findings</Link>
          <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/changes?${new URLSearchParams({ source: source.id }).toString()}`}>Open change history</Link>
          {source.last_comparison_id ? <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/comparisons/${source.last_comparison_id}`}>Open latest comparison</Link> : null}
        </div>
      </section>

      <section aria-labelledby="source-scope-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-scope-title">Declared target scope</h3><p>This scope is part of baseline compatibility and absence interpretation.</p></div></div>
        <pre className="monitoring-json-view">{JSON.stringify(source.target_scope || {}, null, 2)}</pre>
      </section>

      <section aria-labelledby="source-config-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-config-title">Monitoring configuration</h3><p>Automatic monitoring controls freshness, automatic comparisons, and finding policy evaluation. Disabling it does not block manual ingestion or comparison. Cadence never remotely schedules a collector.</p></div></div>
        {canManage ? <form className="monitoring-action-form" onSubmit={(event) => void submit(event)}>{serverConfigurationChanged ? <StatusBanner tone="warning" title="Source settings changed in another session"><p>Your unsaved draft is preserved. Load the latest settings before editing again so a newer administrator change is not overwritten.</p><button className="inventory-button-secondary mt-2" onClick={() => loadSourceDraft(source)} type="button">Discard draft and load latest</button></StatusBanner> : null}<label>Display name<input aria-invalid={nameInvalid} disabled={busy || serverConfigurationChanged} maxLength={255} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label><label>Expected interval (seconds)<input aria-invalid={intervalInvalid} disabled={busy || serverConfigurationChanged} inputMode="numeric" max="31536000" min="300" onChange={(event) => setInterval(event.target.value)} placeholder="Unset" type="number" value={interval} /></label><label className="monitoring-checkbox"><input checked={enabled} disabled={busy || serverConfigurationChanged} onChange={(event) => setEnabled(event.target.checked)} type="checkbox" />Enable automatic monitoring</label>{!enabled ? <p className="monitoring-validation monitoring-disabled-note">Automatic comparison and policy evaluation will be skipped for future ingested runs. Manual ingest and comparison remain available.</p> : null}{nameInvalid ? <p className="monitoring-validation" role="alert">Display name cannot be blank.</p> : null}{intervalInvalid ? <p className="monitoring-validation" role="alert">Expected interval must be a whole number from 300 to 31,536,000 seconds, or left unset.</p> : null}<div className="monitoring-action-row"><button className="inventory-button-primary" disabled={busy || intervalInvalid || nameInvalid || !updateDirty || serverConfigurationChanged} type="submit">{busy ? "Working…" : updateDirty ? "Save source settings" : "No changes to save"}</button></div></form> : permissionError ? <StatusBanner tone="warning" title="Action permissions unavailable"><p>{permissionError} Source configuration remains disabled until the project role can be verified.</p></StatusBanner> : permissionsReady ? <StatusBanner title="Admin permission required"><p>Project admins can rename sources, enable automatic monitoring, and set the expected collection interval. No credentials are managed here.</p></StatusBanner> : <StatePanel description="Checking whether this project role can configure collection monitoring." title="Loading action permissions" />}
      </section>
    </article>
  );
}

export function SourcesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [provider, setProvider] = useState(() => readParam("provider").toLowerCase());
  const [health, setHealth] = useState<SourceHealth | "">(() => normalizeHealth(readParam("health")));
  const [query, setQuery] = useState(() => readParam("q"));
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  const [cursor, setCursor] = useState<string | null>(() => readParam("cursor") || null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>(() => cursor ? [null] : []);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState(() => readParam("source"));
  const [sources, setSources] = useState<MonitoringSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [detail, setDetail] = useState<MonitoringSource | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailNonce, setDetailNonce] = useState(0);
  const [role, setRole] = useState<string | null>(null);
  const [roleReady, setRoleReady] = useState(false);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationInfo, setMutationInfo] = useState<string | null>(null);

  useEffect(() => { const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 300); return () => window.clearTimeout(timer); }, [query]);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    provider ? next.set("provider", provider) : next.delete("provider");
    health ? next.set("health", health) : next.delete("health");
    debouncedQuery ? next.set("q", debouncedQuery) : next.delete("q");
    cursor ? next.set("cursor", cursor) : next.delete("cursor");
    selectedId ? next.set("source", selectedId) : next.delete("source");
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [cursor, debouncedQuery, health, provider, searchParams, selectedId, setSearchParams]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setRoleReady(false);
    setRoleError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/my-role`, { signal: controller.signal }).then((data) => !controller.signal.aborted && setRole(typeof data?.role === "string" ? data.role : null)).catch((caught) => { if (!controller.signal.aborted) { setRole(null); setRoleError(caught instanceof Error ? caught.message : "Project role could not be verified."); } }).finally(() => !controller.signal.aborted && setRoleReady(true));
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (provider) params.set("provider", provider);
    if (health) params.set("health_status", health);
    if (debouncedQuery) params.set("q", debouncedQuery);
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/sources?${params.toString()}`, { signal: controller.signal }).then((data) => {
      if (controller.signal.aborted) return;
      setSources(Array.isArray(data?.items) ? data.items as MonitoringSource[] : []);
      setNextCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
      setLastLoadedAt(new Date());
    }).catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Sources could not be loaded."); }).finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [cursor, debouncedQuery, health, projectId, provider, reloadNonce]);

  useEffect(() => {
    if (!projectId || !selectedId) { setDetail(null); setDetailError(null); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(selectedId)}`, { signal: controller.signal }).then((data) => !controller.signal.aborted && setDetail(data as MonitoringSource)).catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) { setDetailError(caught instanceof Error ? caught.message : "Source detail could not be loaded."); } }).finally(() => !controller.signal.aborted && setDetailLoading(false));
    return () => controller.abort();
  }, [detailNonce, projectId, selectedId]);

  useEffect(() => {
    const monitoringActive = monitoringEvaluationIsActive(detail?.coverage?.monitoring_findings)
      || automaticBaselineIsActive(detail?.coverage?.automatic_baseline);
    if (!monitoringActive || detailLoading || loading) return;
    // Schedule only after the previous detail request has settled. A fixed
    // interval could repeatedly abort a slow but otherwise healthy request.
    const timer = window.setTimeout(() => {
      setDetailNonce((value) => value + 1);
      setReloadNonce((value) => value + 1);
    }, detailError ? 15_000 : 5_000);
    return () => window.clearTimeout(timer);
  }, [
    detail?.id,
    detail?.coverage?.automatic_baseline?.findings_evaluation_state,
    detail?.coverage?.automatic_baseline?.state,
    detail?.coverage?.monitoring_findings?.state,
    detailError,
    detailLoading,
    loading,
  ]);

  function resetPage() { setCursor(null); setCursorHistory([]); }

  async function updateSource(payload: Record<string, unknown>): Promise<MonitoringSource | null> {
    if (!projectId || !detail) return null;
    setMutationBusy(true); setMutationError(null); setMutationInfo(null);
    try {
      const updated = await apiFetch(`/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(detail.id)}`, { method: "PATCH", body: JSON.stringify(payload) }) as MonitoringSource;
      setDetail(updated);
      setSources((rows) => rows.map((row) => row.id === updated.id ? updated : row));
      setMutationInfo("Source monitoring settings saved. Automatic monitoring behavior was updated without changing collector scheduling or credentials.");
      setReloadNonce((value) => value + 1);
      return updated;
    } catch (caught) {
      setMutationError(caught instanceof Error ? caught.message : "Source settings could not be saved.");
      setDetailNonce((value) => value + 1);
      return null;
    } finally { setMutationBusy(false); }
  }

  async function retryFindingEvaluation() {
    const runId = detail?.coverage?.monitoring_findings?.run_id;
    if (!projectId || !detail || !runId || !canRetrySourceMonitoring(detail, role)) return;
    setMutationBusy(true); setMutationError(null); setMutationInfo(null);
    try {
      const response = await apiFetch(
        `/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/monitoring/retry`,
        { method: "POST" },
      );
      const state = typeof response?.state === "string" ? humanizeMonitoringValue(response.state) : "Queued";
      setMutationInfo(`Finding evaluation recovery is ${state.toLowerCase()}. Source health will refresh automatically.`);
    } catch (caught) {
      setMutationError(caught instanceof Error ? caught.message : "Finding evaluation recovery could not be requested.");
    } finally {
      setMutationBusy(false);
      setDetailNonce((value) => value + 1);
      setReloadNonce((value) => value + 1);
    }
  }

  return (
    <section className="monitoring-workspace">
      <header className="monitoring-page-header"><div><p>Collection operations</p><h1>Sources</h1><span>Verify which scopes are monitored, whether their evidence is fresh, and why collection confidence is degraded.</span></div><div className="monitoring-freshness"><strong>{lastLoadedAt ? `Updated ${lastLoadedAt.toLocaleTimeString()}` : "Not loaded"}</strong><span>Metadata only · no credentials stored</span></div></header>

      <section aria-label="Source filters" className="monitoring-filter-bar"><label>Search<input onChange={(event) => { setQuery(event.target.value); resetPage(); }} placeholder="Name, provider, identity, or scope" type="search" value={query} /></label><label>Provider<select onChange={(event) => { setProvider(event.target.value); resetPage(); }} value={provider}><option value="">All providers</option>{PROVIDERS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label><label>Health<select onChange={(event) => { setHealth(normalizeHealth(event.target.value)); resetPage(); }} value={health}><option value="">All health states</option>{SOURCE_HEALTH.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>{(provider || health || query) ? <button className="inventory-button-secondary" onClick={() => { setProvider(""); setHealth(""); setQuery(""); resetPage(); }} type="button">Clear filters</button> : null}</section>

      {mutationError ? <StatusBanner tone="error" title="Source action failed"><p>{mutationError} Current source state is being reloaded before another action is attempted.</p></StatusBanner> : null}
      {mutationInfo ? <StatusBanner tone="success" title="Source monitoring updated"><p>{mutationInfo}</p></StatusBanner> : null}

      <div className="monitoring-split-layout">
        <section aria-busy={loading} aria-labelledby="source-list-title" className="monitoring-queue"><header className="monitoring-queue-header"><div><h2 id="source-list-title">Registered sources</h2><p>Source identity is derived from normalized provider, target scope, and assessed identity context.</p></div><span>Page {cursorHistory.length + 1}</span></header>
          {error ? <StatePanel actions={<button className="inventory-button-primary" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry sources</button>} description={`${error} No source health conclusions are shown.`} title="Sources unavailable" tone="error" /> : null}
          {loading ? <div aria-label="Loading sources" className="inventory-skeleton" role="status">{Array.from({ length: 7 }, (_, index) => <span key={index} />)}</div> : null}
          {!loading && !error && sources.length === 0 ? <StatePanel description={provider || health || debouncedQuery ? "No registered sources match the current server-side filters." : "No source has been registered yet. Sources are created automatically when normalized collection context is ingested."} title="No sources in view" /> : null}
          {!loading && !error && sources.length > 0 ? <div className="monitoring-table-scroll"><table className="monitoring-table"><caption className="sr-only">Registered collection sources</caption><thead><tr><th>Health</th><th>Source</th><th>Provider</th><th>Coverage</th><th>Freshness</th><th>Last success</th></tr></thead><tbody>{sources.map((source) => <tr className={selectedId === source.id ? "is-selected" : ""} key={source.id}><td><span className={`source-health is-${source.health_status}`}>{humanizeMonitoringValue(source.health_status)}</span></td><td><button className="monitoring-row-title" onClick={() => setSelectedId(source.id)} type="button"><strong>{source.display_name}</strong><span title={source.source_key}>{source.assessed_identity || source.source_key}</span></button></td><td><ProviderBadge provider={source.provider} /></td><td><span className={`evidence-state is-${source.coverage?.state === "complete" ? "exact" : source.coverage?.state === "partial" ? "bounded" : "indeterminate"}`}>{humanizeMonitoringValue(source.coverage?.state)}</span></td><td><span className={`source-freshness is-${source.freshness?.state}`}>{humanizeMonitoringValue(source.freshness?.state)}</span><small>Age {formatDuration(source.freshness?.age_seconds)}</small></td><td>{formatMonitoringTimestamp(source.last_success_at)}</td></tr>)}</tbody></table></div> : null}
          <footer className="monitoring-pagination"><span>{sources.length.toLocaleString()} source{sources.length === 1 ? "" : "s"} loaded on this page.</span><nav aria-label="Source pages"><button disabled={cursorHistory.length === 0 || loading} onClick={() => { const previous = cursorHistory[cursorHistory.length - 1] ?? null; setCursorHistory((values) => values.slice(0, -1)); setCursor(previous); }} type="button">Previous</button><strong aria-current="page">{cursorHistory.length + 1}</strong><button disabled={!nextCursor || loading} onClick={() => { setCursorHistory((values) => [...values, cursor]); setCursor(nextCursor); }} type="button">Next</button></nav></footer>
        </section>
        <aside aria-label="Selected source details" className="monitoring-detail-pane"><SourceDetail busy={mutationBusy} canManage={canManageSources(role)} canRetryFindings={canRetrySourceMonitoring(detail, role)} error={detailError} loading={detailLoading} onReload={() => setDetailNonce((value) => value + 1)} onRetryFindings={retryFindingEvaluation} onUpdate={updateSource} permissionError={roleError} permissionsReady={roleReady} source={detail} /></aside>
      </div>
    </section>
  );
}
