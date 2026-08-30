import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  type ItemChangePage,
  type ItemChangeType,
  type ItemComparisonChange,
  type ItemComparisonSnapshot,
} from "@/lib/comparisons";
import { humanizeEvidenceValue } from "@/lib/access-evidence";

const PAGE_LIMIT = 100;
const ITEM_CHANGE_TYPES: ItemChangeType[] = ["added", "removed", "moved", "renamed", "metadata_changed", "permission_changed", "indeterminate"];

function readParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function normalizeType(value: string): ItemChangeType | "" {
  return ITEM_CHANGE_TYPES.includes(value as ItemChangeType) ? value as ItemChangeType : "";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function itemPath(snapshot: ItemComparisonSnapshot | null | undefined): string {
  return snapshot?.path || snapshot?.name || "Path not recorded";
}

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "Not recorded";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

export function ComparisonItemChanges({ projectId, comparisonId }: { projectId: string; comparisonId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [changeType, setChangeType] = useState<ItemChangeType | "">(() => normalizeType(readParam("itemType")));
  const [query, setQuery] = useState(() => readParam("itemQ"));
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  const [cursor, setCursor] = useState<string | null>(() => readParam("itemCursor") || null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>(() => cursor ? [null] : []);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [rows, setRows] = useState<ItemComparisonChange[]>([]);
  const [interpretation, setInterpretation] = useState<ItemChangePage["interpretation"]>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => { const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 300); return () => window.clearTimeout(timer); }, [query]);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    changeType ? next.set("itemType", changeType) : next.delete("itemType");
    debouncedQuery ? next.set("itemQ", debouncedQuery) : next.delete("itemQ");
    cursor ? next.set("itemCursor", cursor) : next.delete("itemCursor");
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [changeType, cursor, debouncedQuery, searchParams, setSearchParams]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (changeType) params.set("change_type", changeType);
    if (debouncedQuery) params.set("q", debouncedQuery);
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/comparisons/${encodeURIComponent(comparisonId)}/item-changes?${params.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const page = (data || {}) as ItemChangePage;
        setRows(Array.isArray(page.items) ? page.items : []);
        setNextCursor(page.next_cursor || null);
        setInterpretation(page.interpretation || null);
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Item changes could not be loaded.");
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [changeType, comparisonId, cursor, debouncedQuery, projectId, reloadNonce]);

  function resetPage() {
    setCursor(null);
    setCursorHistory([]);
  }

  return (
    <>
      <StatusBanner tone={interpretation?.exact ? "success" : interpretation?.state === "not_computed" ? "warning" : "info"} title={interpretation?.exact ? "Item history is exact for the published scope" : "Item history has interpretation limits"}>
        <p>{interpretation?.state === "not_computed" ? "Item-level history was not materialized for this comparison. Empty results do not mean zero item changes." : interpretation?.exact ? "Added, removed, moved, renamed, metadata, and permission changes can be reviewed as durable rows." : "Review evidence state and limitations on each row before treating an absence or permission change as definitive."}</p>
        {(interpretation?.limitations || []).length > 0 ? <ul className="mt-1 list-disc pl-5">{interpretation?.limitations?.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul> : null}
      </StatusBanner>

      <section aria-label="Item change filters" className="comparison-filter-bar comparison-item-filter-bar">
        <div className="comparison-query-filters">
          <label>Search item history<input onChange={(event) => { setQuery(event.target.value); resetPage(); }} placeholder="Path, name, or provider item ID" type="search" value={query} /></label>
          <label>Change type<select onChange={(event) => { setChangeType(normalizeType(event.target.value)); resetPage(); }} value={changeType}><option value="">All item changes</option>{ITEM_CHANGE_TYPES.map((value) => <option key={value} value={value}>{humanizeEvidenceValue(value)}</option>)}</select></label>
          {(changeType || query) ? <button className="inventory-button-secondary" onClick={() => { setChangeType(""); setQuery(""); resetPage(); }} type="button">Clear filters</button> : null}
        </div>
      </section>

      <section aria-busy={loading} aria-labelledby="item-changes-title" className="comparison-results">
        <header><div><h2 id="item-changes-title">Item change history</h2><p>Server-filtered materialized rows. Paths are evidence snapshots, not live file-system checks.</p></div><span>Page {cursorHistory.length + 1} · up to {PAGE_LIMIT} rows</span></header>
        {error ? <StatusBanner tone="error" title="Item changes unavailable"><p>{error} No item-level conclusion is shown.</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry item changes</button></StatusBanner> : null}
        {loading ? <div aria-label="Loading item changes" className="inventory-skeleton" role="status">{Array.from({ length: 8 }, (_, index) => <span key={index} />)}</div> : null}
        {!loading && !error && rows.length === 0 ? <StatePanel description={changeType || debouncedQuery ? "No item changes match the current server-side filters." : interpretation?.state === "not_computed" ? "Item-level changes were not materialized for this comparison. This is not a claim of zero change." : "No item changes were published within the comparable scope."} title="No item changes in view" /> : null}
        {!loading && !error && rows.length > 0 ? <div className="comparison-table-scroll"><table className="comparison-table comparison-item-table"><caption className="sr-only">Materialized item changes between the baseline and current run</caption><thead><tr><th>Change</th><th>Before</th><th>After</th><th>Provider</th><th>Evidence</th><th>Details</th><th>Match</th><th><span className="sr-only">Action</span></th></tr></thead><tbody>{rows.map((row) => {
          const beforePath = itemPath(row.before);
          const afterPath = itemPath(row.after);
          const inventoryQuery = row.after?.path || row.before?.path || row.after?.name || row.before?.name || "";
          return <tr key={row.id}><td><span className={`comparison-change-type is-${row.change_type === "added" ? "positive" : row.change_type === "removed" ? "negative" : row.change_type === "indeterminate" ? "warning" : "neutral"}`}>{humanizeEvidenceValue(row.change_type)}</span></td><td><strong className="comparison-item-path" title={beforePath}>{beforePath}</strong>{row.before ? <small>{row.before.is_dir ? "Directory" : formatBytes(row.before.size_bytes)}</small> : <small>Not present</small>}</td><td><strong className="comparison-item-path" title={afterPath}>{afterPath}</strong>{row.after ? <small>{row.after.is_dir ? "Directory" : formatBytes(row.after.size_bytes)}</small> : <small>Not present</small>}</td><td><ProviderBadge provider={row.provider || "unknown"} /></td><td><span className={`evidence-state is-${row.evidence_state}`}>{humanizeEvidenceValue(row.evidence_state)}</span></td><td><div className="comparison-category-list">{(row.change_categories || []).map((category) => <span key={category}>{humanizeEvidenceValue(category)}</span>)}</div>{(row.limitations || []).length > 0 ? <small title={row.limitations?.join(" ")}>{row.limitations?.length} limitation{row.limitations?.length === 1 ? "" : "s"}</small> : null}</td><td><div className="comparison-match"><strong>{humanizeEvidenceValue(row.match?.quality)}</strong><small>{humanizeEvidenceValue(row.match?.basis)}</small></div></td><td>{inventoryQuery ? <Link className="comparison-inspect-button" to={`/projects/${projectId}/inventory?${new URLSearchParams({ q: inventoryQuery }).toString()}`}>Find in inventory</Link> : null}</td></tr>;
        })}</tbody></table></div> : null}
        <footer className="inventory-pagination"><span>Opaque cursor pagination preserves stable server order.</span><nav aria-label="Item change pages" className="inventory-page-controls"><button disabled={cursorHistory.length === 0 || loading} onClick={() => { const previous = cursorHistory[cursorHistory.length - 1] ?? null; setCursorHistory((values) => values.slice(0, -1)); setCursor(previous); }} type="button">Previous</button><span aria-current="page" className="px-2 font-semibold">Page {cursorHistory.length + 1}</span><button disabled={!nextCursor || loading} onClick={() => { setCursorHistory((values) => [...values, cursor]); setCursor(nextCursor); }} type="button">Next</button></nav></footer>
      </section>
    </>
  );
}
