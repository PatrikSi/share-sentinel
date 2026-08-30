import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  comparisonErrorText,
  comparisonRunLabel,
  comparisonStateTone,
  type ComparisonState,
  type ProjectComparison,
} from "@/lib/comparisons";
import { formatMonitoringTimestamp, humanizeMonitoringValue } from "@/lib/monitoring";

const PAGE_LIMIT = 50;
const COMPARISON_STATES: ComparisonState[] = ["queued", "running", "complete", "failed"];
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function readParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function normalizeState(value: string): ComparisonState | "" {
  return COMPARISON_STATES.includes(value as ComparisonState) ? value as ComparisonState : "";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function interpretationCopy(comparison: ProjectComparison): string {
  if (comparison.state === "queued") return "Waiting for comparison capacity";
  if (comparison.state === "running") return comparison.progress?.message || "Materializing resource and item changes";
  if (comparison.state === "failed") return comparisonErrorText(comparison.error) || "Comparison failed";
  const compatibility = comparison.compatibility;
  if (!compatibility?.structural_interpretable) return "Structural conclusions are indeterminate";
  if (!compatibility.content_interpretable || !compatibility.access_interpretable) return "Some evidence dimensions are limited";
  return "Published comparison is interpretable within collected scope";
}

export function ChangesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [state, setState] = useState<ComparisonState | "">(() => normalizeState(readParam("state")));
  const [sourceId, setSourceId] = useState(() => readParam("source"));
  const [cursor, setCursor] = useState<string | null>(() => readParam("cursor") || null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>(() => cursor ? [null] : []);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [comparisons, setComparisons] = useState<ProjectComparison[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const sourceIdInvalid = !!sourceId && !UUID_PATTERN.test(sourceId);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    state ? next.set("state", state) : next.delete("state");
    sourceId ? next.set("source", sourceId) : next.delete("source");
    cursor ? next.set("cursor", cursor) : next.delete("cursor");
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [cursor, searchParams, setSearchParams, sourceId, state]);

  useEffect(() => {
    if (!projectId) return;
    if (sourceIdInvalid) {
      setComparisons([]);
      setNextCursor(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (state) params.set("state", state);
    if (sourceId.trim()) params.set("source_id", sourceId.trim());
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/comparisons?${params.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setComparisons(Array.isArray(data?.items) ? data.items as ProjectComparison[] : []);
        setNextCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
        setLastLoadedAt(new Date());
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Comparison history could not be loaded.");
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [cursor, projectId, reloadNonce, sourceId, sourceIdInvalid, state]);

  function resetPage() {
    setCursor(null);
    setCursorHistory([]);
  }

  return (
    <section className="monitoring-workspace">
      <header className="monitoring-page-header">
        <div><p>Continuous monitoring</p><h1>Changes</h1><span>Review durable comparison history and open resource- or item-level change evidence without returning to individual runs.</span></div>
        <div className="monitoring-freshness"><strong>{lastLoadedAt ? `Updated ${lastLoadedAt.toLocaleTimeString()}` : "Not loaded"}</strong><span>Server-filtered · newest first</span></div>
      </header>

      <section aria-label="Comparison filters" className="monitoring-filter-bar">
        <label>State<select onChange={(event) => { setState(normalizeState(event.target.value)); resetPage(); }} value={state}><option value="">All states</option>{COMPARISON_STATES.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>
        <label>Source ID<input onChange={(event) => { setSourceId(event.target.value.trim()); resetPage(); }} placeholder="Optional stable source ID" value={sourceId} /></label>
        {(state || sourceId) ? <button className="inventory-button-secondary" onClick={() => { setState(""); setSourceId(""); resetPage(); }} type="button">Clear filters</button> : null}
      </section>

      {sourceIdInvalid ? <StatusBanner tone="warning" title="Source filter is incomplete"><p>Enter a complete source UUID or clear the filter. No server request is sent for an invalid identifier.</p></StatusBanner> : sourceId ? <StatusBanner title="Source-scoped history"><p>Showing comparisons registered to source <code>{sourceId}</code>. Open Sources to inspect its coverage and freshness.</p></StatusBanner> : null}

      <section aria-busy={loading} aria-labelledby="comparison-history-title" className="monitoring-queue">
        <header className="monitoring-queue-header"><div><h2 id="comparison-history-title">Comparison history</h2><p>Each row is a materialized, immutable comparison attempt with explicit interpretation limits.</p></div><span>Page {cursorHistory.length + 1}</span></header>
        {error ? <StatePanel actions={<button className="inventory-button-primary" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry history</button>} description={`${error} Previously opened comparisons remain addressable by URL.`} title="Comparison history unavailable" tone="error" /> : null}
        {loading ? <div aria-label="Loading comparisons" className="inventory-skeleton" role="status">{Array.from({ length: 8 }, (_, index) => <span key={index} />)}</div> : null}
        {!sourceIdInvalid && !loading && !error && comparisons.length === 0 ? <StatePanel description={state || sourceId ? "No comparisons match the current server-side filters." : "No materialized comparisons exist yet. A comparable successful run can be evaluated against its previous source baseline."} title="No comparisons in view" /> : null}
        {!loading && !error && comparisons.length > 0 ? (
          <div className="monitoring-table-scroll">
            <table className="monitoring-table comparison-history-table">
              <caption className="sr-only">Materialized project comparisons</caption>
              <thead><tr><th>State</th><th>Baseline</th><th>Current</th><th>Resource changes</th><th>Interpretation</th><th>Created</th><th><span className="sr-only">Action</span></th></tr></thead>
              <tbody>{comparisons.map((comparison) => {
                const summary = comparison.summary;
                return <tr key={comparison.id}><td><span className={`comparison-state is-${comparisonStateTone(comparison.state)}`}>{humanizeMonitoringValue(comparison.state)}</span></td><td><strong>{comparisonRunLabel(comparison, "baseline")}</strong><small>{formatMonitoringTimestamp(comparison.baseline_run?.created_at)}</small></td><td><strong>{comparisonRunLabel(comparison, "current")}</strong><small>{formatMonitoringTimestamp(comparison.current_run?.created_at)}</small></td><td>{summary ? <div className="monitoring-change-counts"><span><strong>{summary.appeared.toLocaleString()}</strong> appeared</span><span><strong>{summary.disappeared.toLocaleString()}</strong> disappeared</span><span><strong>{summary.changed.toLocaleString()}</strong> changed</span><span><strong>{summary.indeterminate.toLocaleString()}</strong> indeterminate</span></div> : <span>Not published</span>}</td><td><p className="monitoring-interpretation">{interpretationCopy(comparison)}</p>{comparison.compatibility?.reasons?.length ? <small>{comparison.compatibility.reasons.length.toLocaleString()} recorded limitation{comparison.compatibility.reasons.length === 1 ? "" : "s"}</small> : null}</td><td>{formatMonitoringTimestamp(comparison.created_at)}</td><td><Link className="inventory-button-primary" to={`/projects/${projectId}/comparisons/${comparison.id}`}>{comparison.state === "complete" ? "Inspect changes" : "Open status"}</Link></td></tr>;
              })}</tbody>
            </table>
          </div>
        ) : null}
        <footer className="monitoring-pagination"><span>{comparisons.length.toLocaleString()} comparison{comparisons.length === 1 ? "" : "s"} loaded on this page.</span><nav aria-label="Comparison history pages"><button disabled={cursorHistory.length === 0 || loading} onClick={() => { const previous = cursorHistory[cursorHistory.length - 1] ?? null; setCursorHistory((values) => values.slice(0, -1)); setCursor(previous); }} type="button">Previous</button><strong aria-current="page">{cursorHistory.length + 1}</strong><button disabled={!nextCursor || loading} onClick={() => { setCursorHistory((values) => [...values, cursor]); setCursor(nextCursor); }} type="button">Next</button></nav></footer>
      </section>
    </section>
  );
}
