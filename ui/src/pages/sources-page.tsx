import { FormEvent, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  canManageSources,
  formatDuration,
  formatMonitoringTimestamp,
  humanizeMonitoringValue,
  type MonitoringSource,
  type SourceHealth,
} from "@/lib/monitoring";

const PAGE_LIMIT = 50;
const SOURCE_HEALTH: SourceHealth[] = ["healthy", "stale", "degraded", "never_collected", "disabled"];
const PROVIDERS = ["smb", "sharepoint", "nfs"];

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

function SourceDetail({ source, loading, error, canManage, busy, onReload, onUpdate }: {
  source: MonitoringSource | null;
  loading: boolean;
  error: string | null;
  canManage: boolean;
  busy: boolean;
  onReload: () => void;
  onUpdate: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [displayName, setDisplayName] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [interval, setInterval] = useState("");

  useEffect(() => {
    if (!source) return;
    setDisplayName(source.display_name || "");
    setEnabled(source.enabled);
    setInterval(source.expected_interval_seconds == null ? "" : String(source.expected_interval_seconds));
  }, [source?.id, source?.updated_at]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!source) return;
    const parsedInterval = interval.trim() === "" ? null : Number(interval);
    if (parsedInterval !== null && (!Number.isSafeInteger(parsedInterval) || parsedInterval <= 0)) return;
    await onUpdate({ display_name: displayName.trim(), enabled, expected_interval_seconds: parsedInterval });
  }

  if (loading) return <StatePanel description="Loading source scope, cadence, and collection health." title="Loading source" />;
  if (error) return <StatePanel actions={<button className="inventory-button-primary" onClick={onReload} type="button">Retry source</button>} description={`${error} Retrying this read does not trigger collection.`} title="Source unavailable" tone="error" />;
  if (!source) return <StatePanel description="Choose a source to inspect collection scope, coverage, freshness, and configuration." title="Select a source" />;

  const coverageReasons = source.coverage?.reasons || [];
  const healthReasons = source.health_reasons || [];
  const intervalInvalid = interval.trim() !== "" && (!Number.isSafeInteger(Number(interval)) || Number(interval) <= 0);
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

      <section aria-labelledby="source-actions-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-actions-title">Investigation paths</h3><p>Use source-scoped history before treating a missing resource as a confirmed disappearance.</p></div></div>
        <div className="monitoring-action-row">
          {source.last_run_id ? <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/runs/${source.last_run_id}`}>Open latest run</Link> : null}
          <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/changes?${new URLSearchParams({ source: source.id }).toString()}`}>Open change history</Link>
          {source.last_comparison_id ? <Link className="inventory-button-secondary" to={`/projects/${source.project_id}/comparisons/${source.last_comparison_id}`}>Open latest comparison</Link> : null}
        </div>
      </section>

      <section aria-labelledby="source-scope-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-scope-title">Declared target scope</h3><p>This scope is part of baseline compatibility and absence interpretation.</p></div></div>
        <pre className="monitoring-json-view">{JSON.stringify(source.target_scope || {}, null, 2)}</pre>
      </section>

      <section aria-labelledby="source-config-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="source-config-title">Monitoring configuration</h3><p>Changing cadence affects freshness evaluation only; it does not remotely schedule or run a collector.</p></div></div>
        {canManage ? <form className="monitoring-action-form" onSubmit={(event) => void submit(event)}><label>Display name<input disabled={busy} maxLength={200} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label><label>Expected interval (seconds)<input disabled={busy} inputMode="numeric" min="1" onChange={(event) => setInterval(event.target.value)} placeholder="Unset" type="number" value={interval} /></label><label className="monitoring-checkbox"><input checked={enabled} disabled={busy} onChange={(event) => setEnabled(event.target.checked)} type="checkbox" />Enable freshness monitoring</label>{intervalInvalid ? <p className="monitoring-validation" role="alert">Expected interval must be a positive whole number of seconds or left unset.</p> : null}<div className="monitoring-action-row"><button className="inventory-button-primary" disabled={busy || intervalInvalid} type="submit">{busy ? "Saving…" : "Save source settings"}</button></div></form> : <StatusBanner title="Admin permission required"><p>Project admins can rename sources, enable monitoring, and set the expected collection interval. No credentials are managed here.</p></StatusBanner>}
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
    apiFetch(`/projects/${encodeURIComponent(projectId)}/my-role`, { signal: controller.signal }).then((data) => !controller.signal.aborted && setRole(typeof data?.role === "string" ? data.role : null)).catch(() => !controller.signal.aborted && setRole(null));
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
    apiFetch(`/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(selectedId)}`, { signal: controller.signal }).then((data) => !controller.signal.aborted && setDetail(data as MonitoringSource)).catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) { setDetail(null); setDetailError(caught instanceof Error ? caught.message : "Source detail could not be loaded."); } }).finally(() => !controller.signal.aborted && setDetailLoading(false));
    return () => controller.abort();
  }, [detailNonce, projectId, selectedId]);

  function resetPage() { setCursor(null); setCursorHistory([]); }

  async function updateSource(payload: Record<string, unknown>) {
    if (!projectId || !detail) return;
    setMutationBusy(true); setMutationError(null); setMutationInfo(null);
    try {
      const updated = await apiFetch(`/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(detail.id)}`, { method: "PATCH", body: JSON.stringify(payload) }) as MonitoringSource;
      setDetail(updated);
      setSources((rows) => rows.map((row) => row.id === updated.id ? updated : row));
      setMutationInfo("Source monitoring settings saved. Collection scheduling and credentials were not changed.");
      setReloadNonce((value) => value + 1);
    } catch (caught) {
      setMutationError(caught instanceof Error ? caught.message : "Source settings could not be saved.");
      setDetailNonce((value) => value + 1);
    } finally { setMutationBusy(false); }
  }

  return (
    <section className="monitoring-workspace">
      <header className="monitoring-page-header"><div><p>Collection operations</p><h1>Sources</h1><span>Verify which scopes are monitored, whether their evidence is fresh, and why collection confidence is degraded.</span></div><div className="monitoring-freshness"><strong>{lastLoadedAt ? `Updated ${lastLoadedAt.toLocaleTimeString()}` : "Not loaded"}</strong><span>Metadata only · no credentials stored</span></div></header>

      <section aria-label="Source filters" className="monitoring-filter-bar"><label>Search<input onChange={(event) => { setQuery(event.target.value); resetPage(); }} placeholder="Name, source key, or identity" type="search" value={query} /></label><label>Provider<select onChange={(event) => { setProvider(event.target.value); resetPage(); }} value={provider}><option value="">All providers</option>{PROVIDERS.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label><label>Health<select onChange={(event) => { setHealth(normalizeHealth(event.target.value)); resetPage(); }} value={health}><option value="">All health states</option>{SOURCE_HEALTH.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>{(provider || health || query) ? <button className="inventory-button-secondary" onClick={() => { setProvider(""); setHealth(""); setQuery(""); resetPage(); }} type="button">Clear filters</button> : null}</section>

      {mutationError ? <StatusBanner tone="error" title="Source update failed"><p>{mutationError} The last confirmed configuration remains visible.</p></StatusBanner> : null}
      {mutationInfo ? <StatusBanner tone="success" title="Source updated"><p>{mutationInfo}</p></StatusBanner> : null}

      <div className="monitoring-split-layout">
        <section aria-busy={loading} aria-labelledby="source-list-title" className="monitoring-queue"><header className="monitoring-queue-header"><div><h2 id="source-list-title">Registered sources</h2><p>Source identity is derived from normalized provider, target scope, and assessed identity context.</p></div><span>Page {cursorHistory.length + 1}</span></header>
          {error ? <StatePanel actions={<button className="inventory-button-primary" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry sources</button>} description={`${error} No source health conclusions are shown.`} title="Sources unavailable" tone="error" /> : null}
          {loading ? <div aria-label="Loading sources" className="inventory-skeleton" role="status">{Array.from({ length: 7 }, (_, index) => <span key={index} />)}</div> : null}
          {!loading && !error && sources.length === 0 ? <StatePanel description={provider || health || debouncedQuery ? "No registered sources match the current server-side filters." : "No source has been registered yet. Sources are created automatically when normalized collection context is ingested."} title="No sources in view" /> : null}
          {!loading && !error && sources.length > 0 ? <div className="monitoring-table-scroll"><table className="monitoring-table"><caption className="sr-only">Registered collection sources</caption><thead><tr><th>Health</th><th>Source</th><th>Provider</th><th>Coverage</th><th>Freshness</th><th>Last success</th></tr></thead><tbody>{sources.map((source) => <tr className={selectedId === source.id ? "is-selected" : ""} key={source.id}><td><span className={`source-health is-${source.health_status}`}>{humanizeMonitoringValue(source.health_status)}</span></td><td><button className="monitoring-row-title" onClick={() => setSelectedId(source.id)} type="button"><strong>{source.display_name}</strong><span title={source.source_key}>{source.assessed_identity || source.source_key}</span></button></td><td><ProviderBadge provider={source.provider} /></td><td><span className={`evidence-state is-${source.coverage?.state === "complete" ? "exact" : source.coverage?.state === "partial" ? "bounded" : "indeterminate"}`}>{humanizeMonitoringValue(source.coverage?.state)}</span></td><td><span className={`source-freshness is-${source.freshness?.state}`}>{humanizeMonitoringValue(source.freshness?.state)}</span><small>Age {formatDuration(source.freshness?.age_seconds)}</small></td><td>{formatMonitoringTimestamp(source.last_success_at)}</td></tr>)}</tbody></table></div> : null}
          <footer className="monitoring-pagination"><span>{sources.length.toLocaleString()} source{sources.length === 1 ? "" : "s"} loaded on this page.</span><nav aria-label="Source pages"><button disabled={cursorHistory.length === 0 || loading} onClick={() => { const previous = cursorHistory[cursorHistory.length - 1] ?? null; setCursorHistory((values) => values.slice(0, -1)); setCursor(previous); }} type="button">Previous</button><strong aria-current="page">{cursorHistory.length + 1}</strong><button disabled={!nextCursor || loading} onClick={() => { setCursorHistory((values) => [...values, cursor]); setCursor(nextCursor); }} type="button">Next</button></nav></footer>
        </section>
        <aside aria-label="Selected source details" className="monitoring-detail-pane"><SourceDetail busy={mutationBusy} canManage={canManageSources(role)} error={detailError} loading={detailLoading} onReload={() => setDetailNonce((value) => value + 1)} onUpdate={updateSource} source={detail} /></aside>
      </div>
    </section>
  );
}
