import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type RunInfo = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  target_scope: Record<string, unknown>;
  summary: { endpoints?: number; resources?: number; items?: number; errors?: number };
};
type RunCompareOption = {
  id: string;
  name: string;
  status: string;
  created_at: string;
};

type Endpoint = {
  id: number;
  endpoint_key: string;
  ip: string | null;
  hostname: string | null;
  smb_signing: string | null;
};
type Resource = { id: number; name: string; access_level: string; remark: string | null; share_type: string };
type Item = { id: number; path: string; is_dir: boolean; resource_id?: number; name?: string };
type SavedQuery = { id: string; label: string; q: string; ext: string };
type RunDiffShare = {
  endpoint_key: string;
  hostname: string | null;
  ip: string | null;
  share_name: string;
  share_type: string;
  access_level: string | null;
  item_count: number;
};
type RunDiffChurn = RunDiffShare & {
  added_items: number;
  removed_items: number;
  added_examples: string[];
  removed_examples: string[];
};
type RunDiffResult = {
  current_run: { id: string; name: string; created_at: string | null; status: string };
  baseline_run: { id: string; name: string; created_at: string | null; status: string } | null;
  summary: {
    new_shares: number;
    disappeared_shares: number;
    changed_shares: number;
    added_items: number;
    removed_items: number;
  };
  new_shares: RunDiffShare[];
  disappeared_shares: RunDiffShare[];
  item_churn: RunDiffChurn[];
};

const RUN_STATUS_COLORS: Record<string, string> = {
  PENDING_UPLOAD: "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-200",
  UPLOADED: "bg-amber-200 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  INGESTING: "bg-sky-200 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  COMPLETE: "bg-emerald-200 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  FAILED: "bg-rose-200 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
};

export function RunDetailPage() {
  const { projectId, runId } = useParams<{ projectId: string; runId: string }>();

  const [run, setRun] = useState<RunInfo | null>(null);
  const [projectRuns, setProjectRuns] = useState<RunCompareOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [selectedBaselineRunId, setSelectedBaselineRunId] = useState("");
  const [runDiff, setRunDiff] = useState<RunDiffResult | null>(null);

  const [endpointSearch, setEndpointSearch] = useState("");
  const [itemSearch, setItemSearch] = useState("");
  const [pathPrefix, setPathPrefix] = useState("");
  const [globalQuery, setGlobalQuery] = useState("");
  const [globalExt, setGlobalExt] = useState("");

  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [resources, setResources] = useState<Resource[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [globalItems, setGlobalItems] = useState<Item[]>([]);

  const [selectedEndpoint, setSelectedEndpoint] = useState<number | null>(null);
  const [selectedResource, setSelectedResource] = useState<number | null>(null);

  const [endpointCursor, setEndpointCursor] = useState<string | null>(null);
  const [endpointHistory, setEndpointHistory] = useState<Array<string | null>>([]);
  const [endpointNext, setEndpointNext] = useState<string | null>(null);

  const [itemCursor, setItemCursor] = useState<string | null>(null);
  const [itemHistory, setItemHistory] = useState<Array<string | null>>([]);
  const [itemNext, setItemNext] = useState<string | null>(null);

  const [globalCursor, setGlobalCursor] = useState<string | null>(null);
  const [globalHistory, setGlobalHistory] = useState<Array<string | null>>([]);
  const [globalNext, setGlobalNext] = useState<string | null>(null);

  const savedQueriesKey = useMemo(() => `share_sentinel_saved_queries_${runId || "default"}`, [runId]);
  const [savedQueries, setSavedQueries] = useState<SavedQuery[]>([]);
  const [savedQueryLabel, setSavedQueryLabel] = useState("");

  useEffect(() => {
    if (!runId) return;
    const raw = localStorage.getItem(savedQueriesKey);
    if (!raw) {
      setSavedQueries([]);
      return;
    }
    try {
      setSavedQueries(JSON.parse(raw) as SavedQuery[]);
    } catch {
      setSavedQueries([]);
    }
  }, [runId, savedQueriesKey]);

  function persistSavedQueries(next: SavedQuery[]) {
    setSavedQueries(next);
    localStorage.setItem(savedQueriesKey, JSON.stringify(next));
  }

  useEffect(() => {
    if (!projectId || !runId) return;
    apiFetch(`/projects/${projectId}/runs/${runId}`)
      .then((data) => setRun(data as RunInfo))
      .catch((err) => setError(err.message));
  }, [projectId, runId]);

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}/runs?limit=200`)
      .then((data) => setProjectRuns((data?.items || []) as RunCompareOption[]))
      .catch((err) => setError(err.message));
  }, [projectId]);

  const baselineOptions = useMemo(() => {
    if (!runId) return [];
    const currentCreatedAt = run ? new Date(run.created_at).getTime() : Number.POSITIVE_INFINITY;
    return projectRuns.filter((candidate) => {
      if (candidate.id === runId || candidate.status !== "COMPLETE") return false;
      const createdAt = new Date(candidate.created_at).getTime();
      return Number.isFinite(createdAt) ? createdAt <= currentCreatedAt : true;
    });
  }, [projectRuns, run, runId]);

  useEffect(() => {
    setSelectedBaselineRunId("");
    setRunDiff(null);
    setDiffError(null);
  }, [runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    setDiffLoading(true);
    setDiffError(null);
    const query = new URLSearchParams();
    if (selectedBaselineRunId) query.set("baseline_run_id", selectedBaselineRunId);
    const suffix = query.toString() ? `?${query.toString()}` : "";

    apiFetch(`/projects/${projectId}/runs/${runId}/diff${suffix}`)
      .then((data) => {
        const payload = data as RunDiffResult;
        setRunDiff(payload);
        if (!selectedBaselineRunId && payload.baseline_run?.id) {
          setSelectedBaselineRunId(payload.baseline_run.id);
        }
      })
      .catch((err) => setDiffError(err.message))
      .finally(() => setDiffLoading(false));
  }, [projectId, runId, selectedBaselineRunId]);

  useEffect(() => {
    if (!projectId || !runId || !run) return;
    if (run.status !== "UPLOADED" && run.status !== "INGESTING") return;
    const timer = window.setInterval(() => {
      apiFetch(`/projects/${projectId}/runs/${runId}`)
        .then((data) => setRun(data as RunInfo))
        .catch(() => undefined);
    }, 8000);
    return () => window.clearInterval(timer);
  }, [projectId, runId, run]);

  useEffect(() => {
    setEndpointCursor(null);
    setEndpointHistory([]);
  }, [endpointSearch, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "100", search: endpointSearch });
    if (endpointCursor) query.set("cursor", endpointCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints?${query.toString()}`)
      .then((data) => {
        const rows = (data?.items || []) as Endpoint[];
        setEndpoints(rows);
        setEndpointNext((data?.next_cursor as string | null) || null);
        if (!selectedEndpoint && rows.length > 0) {
          setSelectedEndpoint(rows[0].id);
        }
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, endpointSearch, endpointCursor, selectedEndpoint]);

  useEffect(() => {
    if (!projectId || !runId || !selectedEndpoint) return;
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources`)
      .then((data) => {
        const rows = (data?.items || []) as Resource[];
        setResources(rows);
        setSelectedResource(rows.length > 0 ? rows[0].id : null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, selectedEndpoint]);

  useEffect(() => {
    setItemCursor(null);
    setItemHistory([]);
  }, [selectedResource, itemSearch, pathPrefix, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || !selectedResource) return;
    const query = new URLSearchParams({ limit: "200", search: itemSearch });
    if (pathPrefix.trim()) query.set("path_prefix", pathPrefix.trim());
    if (itemCursor) query.set("cursor", itemCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/resources/${selectedResource}/items?${query.toString()}`)
      .then((data) => {
        setItems((data?.items || []) as Item[]);
        setItemNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, selectedResource, itemSearch, pathPrefix, itemCursor]);

  useEffect(() => {
    setGlobalCursor(null);
    setGlobalHistory([]);
  }, [globalQuery, globalExt, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "200", q: globalQuery });
    if (globalExt) query.set("ext", globalExt);
    if (globalCursor) query.set("cursor", globalCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/search/items?${query.toString()}`)
      .then((data) => {
        setGlobalItems((data?.items || []) as Item[]);
        setGlobalNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, globalQuery, globalExt, globalCursor]);

  function moveCursor(
    next: string | null,
    current: string | null,
    setCurrent: (value: string | null) => void,
    setHistory: (fn: (prev: Array<string | null>) => Array<string | null>) => void,
  ) {
    if (!next) return;
    setHistory((prev) => [...prev, current]);
    setCurrent(next);
  }

  function moveBack(
    setCurrent: (value: string | null) => void,
    setHistory: (fn: (prev: Array<string | null>) => Array<string | null>) => void,
  ) {
    setHistory((prev) => {
      if (prev.length === 0) return prev;
      const copy = [...prev];
      const value = copy.pop() ?? null;
      setCurrent(value);
      return copy;
    });
  }

  function saveCurrentQuery() {
    if (!savedQueryLabel.trim()) return;
    const next: SavedQuery[] = [
      ...savedQueries,
      {
        id: crypto.randomUUID(),
        label: savedQueryLabel.trim(),
        q: globalQuery,
        ext: globalExt,
      },
    ];
    persistSavedQueries(next);
    setSavedQueryLabel("");
  }

  function removeSavedQuery(id: string) {
    persistSavedQueries(savedQueries.filter((query) => query.id !== id));
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">{run?.name || "Run Explorer"}</h1>
            <p className="text-sm text-slate-600 dark:text-slate-300">Run ID: {runId}</p>
            {run?.description ? <p className="mt-1 text-xs text-slate-500">{run.description}</p> : null}
            {projectId ? (
              <div className="mt-2">
                <Link
                  className="rounded-lg border border-slate-300 px-2 py-1 text-[11px] font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  to={`/projects/${projectId}/inventory`}
                >
                  Open Project Inventory
                </Link>
              </div>
            ) : null}
          </div>
          {run ? (
            <div className="text-right text-xs">
              <span className={`rounded-full px-2 py-1 font-semibold ${RUN_STATUS_COLORS[run.status] || "bg-slate-200 text-slate-900"}`}>
                {run.status}
              </span>
              <p className="mt-2 text-slate-500">Created {new Date(run.created_at).toLocaleString()}</p>
              <p className="text-slate-500">
                e:{run.summary?.endpoints || 0} r:{run.summary?.resources || 0} i:{run.summary?.items || 0} err:
                {run.summary?.errors || 0}
              </p>
            </div>
          ) : null}
        </div>
        {error ? <p className="mt-2 text-sm text-red-600">{error}</p> : null}
      </div>

      <div className="workspace-section space-y-4">
        <div className="workspace-card">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">Run-to-Run Diff</h2>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Compare this run against a prior complete run to see new shares, disappeared shares, and item churn.
              </p>
            </div>
            <label className="block min-w-[280px] text-xs font-semibold uppercase tracking-wide text-slate-500">
              Baseline run
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={selectedBaselineRunId}
                onChange={(event) => setSelectedBaselineRunId(event.target.value)}
              >
                <option value="">Nearest previous complete run</option>
                {baselineOptions.map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.name} [{candidate.status}] {new Date(candidate.created_at).toLocaleString()}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {diffLoading ? <p className="mt-3 text-sm text-slate-500">Loading run diff…</p> : null}
          {diffError ? <p className="mt-3 text-sm text-red-600">{diffError}</p> : null}

          {runDiff && !diffLoading ? (
            runDiff.baseline_run ? (
              <div className="mt-4 space-y-4">
                <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                  <div className="rounded-lg border border-slate-300 p-3 text-sm dark:border-slate-700">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Baseline</p>
                    <p className="mt-1 font-semibold">{runDiff.baseline_run.name}</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {runDiff.baseline_run.status} •{" "}
                      {runDiff.baseline_run.created_at ? new Date(runDiff.baseline_run.created_at).toLocaleString() : "unknown time"}
                    </p>
                  </div>
                  <div className="rounded-lg border border-slate-300 p-3 text-sm dark:border-slate-700">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Current</p>
                    <p className="mt-1 font-semibold">{runDiff.current_run.name}</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {runDiff.current_run.status} •{" "}
                      {runDiff.current_run.created_at ? new Date(runDiff.current_run.created_at).toLocaleString() : "unknown time"}
                    </p>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                  <div className="rounded-lg border border-slate-300 p-3 dark:border-slate-700">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">New Shares</p>
                    <p className="mt-1 text-2xl font-semibold">{runDiff.summary.new_shares}</p>
                  </div>
                  <div className="rounded-lg border border-slate-300 p-3 dark:border-slate-700">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">Disappeared Shares</p>
                    <p className="mt-1 text-2xl font-semibold">{runDiff.summary.disappeared_shares}</p>
                  </div>
                  <div className="rounded-lg border border-slate-300 p-3 dark:border-slate-700">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">Changed Shares</p>
                    <p className="mt-1 text-2xl font-semibold">{runDiff.summary.changed_shares}</p>
                  </div>
                  <div className="rounded-lg border border-slate-300 p-3 dark:border-slate-700">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">Added Items</p>
                    <p className="mt-1 text-2xl font-semibold">{runDiff.summary.added_items}</p>
                  </div>
                  <div className="rounded-lg border border-slate-300 p-3 dark:border-slate-700">
                    <p className="text-[11px] uppercase tracking-wide text-slate-500">Removed Items</p>
                    <p className="mt-1 text-2xl font-semibold">{runDiff.summary.removed_items}</p>
                  </div>
                </div>
              </div>
            ) : (
              <p className="mt-3 text-sm text-slate-500">No earlier complete run is available for comparison yet.</p>
            )
          ) : null}
        </div>

        {runDiff?.baseline_run ? (
          <div className="grid gap-4 xl:grid-cols-3">
            <div className="workspace-card">
              <h3 className="text-base font-semibold">New Shares</h3>
              <p className="mt-1 text-xs text-slate-500">Shares present now but absent in the baseline run.</p>
              <ul className="mt-3 max-h-[320px] space-y-2 overflow-auto">
                {runDiff.new_shares.length === 0 ? <li className="text-sm text-slate-500">No newly discovered shares.</li> : null}
                {runDiff.new_shares.map((share) => (
                  <li className="rounded-lg border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
                    <div className="font-semibold">{share.share_name}</div>
                    <div className="mt-1 text-slate-500">{share.endpoint_key}</div>
                    <div className="mt-1 text-slate-500">
                      {share.share_type.toUpperCase()} • {share.access_level || "unknown"} • {share.item_count} item(s)
                    </div>
                  </li>
                ))}
              </ul>
            </div>

            <div className="workspace-card">
              <h3 className="text-base font-semibold">Disappeared Shares</h3>
              <p className="mt-1 text-xs text-slate-500">Shares that existed in the baseline run but are gone now.</p>
              <ul className="mt-3 max-h-[320px] space-y-2 overflow-auto">
                {runDiff.disappeared_shares.length === 0 ? <li className="text-sm text-slate-500">No disappeared shares.</li> : null}
                {runDiff.disappeared_shares.map((share) => (
                  <li className="rounded-lg border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
                    <div className="font-semibold">{share.share_name}</div>
                    <div className="mt-1 text-slate-500">{share.endpoint_key}</div>
                    <div className="mt-1 text-slate-500">
                      {share.share_type.toUpperCase()} • {share.access_level || "unknown"} • {share.item_count} item(s)
                    </div>
                  </li>
                ))}
              </ul>
            </div>

            <div className="workspace-card">
              <h3 className="text-base font-semibold">Item Churn</h3>
              <p className="mt-1 text-xs text-slate-500">Shares that remained in scope but changed contents between runs.</p>
              <ul className="mt-3 max-h-[320px] space-y-2 overflow-auto">
                {runDiff.item_churn.length === 0 ? <li className="text-sm text-slate-500">No item churn detected.</li> : null}
                {runDiff.item_churn.map((share) => (
                  <li className="rounded-lg border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
                    <div className="font-semibold">{share.share_name}</div>
                    <div className="mt-1 text-slate-500">{share.endpoint_key}</div>
                    <div className="mt-1 text-slate-500">
                      +{share.added_items} / -{share.removed_items} item(s)
                    </div>
                    {share.added_examples.length > 0 ? (
                      <div className="mt-2">
                        <p className="font-semibold text-emerald-700 dark:text-emerald-300">Added</p>
                        <ul className="mt-1 space-y-1 text-slate-500">
                          {share.added_examples.map((path) => (
                            <li className="font-mono" key={`add:${share.endpoint_key}:${share.share_name}:${path}`}>
                              {path}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    {share.removed_examples.length > 0 ? (
                      <div className="mt-2">
                        <p className="font-semibold text-rose-700 dark:text-rose-300">Removed</p>
                        <ul className="mt-1 space-y-1 text-slate-500">
                          {share.removed_examples.map((path) => (
                            <li className="font-mono" key={`remove:${share.endpoint_key}:${share.share_name}:${path}`}>
                              {path}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}
      </div>

      <div className="workspace-section grid gap-4 md:grid-cols-3">
        <div className="workspace-card">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Endpoints</h2>
            <input
              className="w-44 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search endpoint"
              value={endpointSearch}
              onChange={(event) => setEndpointSearch(event.target.value)}
            />
          </div>
          <div className="mb-2 flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveBack(setEndpointCursor, setEndpointHistory)}
              disabled={endpointHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveCursor(endpointNext, endpointCursor, setEndpointCursor, setEndpointHistory)}
              disabled={!endpointNext}
            >
              Next
            </button>
          </div>
          <ul className="space-y-2">
            {endpoints.map((endpoint) => (
              <li key={endpoint.id}>
                <button
                  className={`w-full rounded-lg border px-2 py-2 text-left text-xs ${
                    selectedEndpoint === endpoint.id
                      ? "border-ember bg-ember/10"
                      : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  }`}
                  onClick={() => setSelectedEndpoint(endpoint.id)}
                >
                  <div className="font-semibold">{endpoint.endpoint_key}</div>
                  <div className="text-slate-500">
                    {(endpoint.hostname || endpoint.ip || "-") + (endpoint.smb_signing ? ` | signing:${endpoint.smb_signing}` : "")}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="workspace-card">
          <h2 className="mb-3 text-lg font-semibold">Shares</h2>
          <ul className="space-y-2">
            {resources.map((resource) => (
              <li key={resource.id}>
                <button
                  className={`w-full rounded-lg border px-2 py-2 text-left text-xs ${
                    selectedResource === resource.id
                      ? "border-pine bg-pine/10"
                      : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  }`}
                  onClick={() => setSelectedResource(resource.id)}
                >
                  <span className="block font-semibold">{resource.name}</span>
                  <span className="text-slate-500">
                    {resource.share_type.toUpperCase()} | {resource.access_level}
                  </span>
                  {resource.remark ? <span className="block text-slate-500">{resource.remark}</span> : null}
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="workspace-card">
          <div className="mb-3 grid gap-2">
            <h2 className="text-lg font-semibold">Items</h2>
            <input
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search name"
              value={itemSearch}
              onChange={(event) => setItemSearch(event.target.value)}
            />
            <input
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Path prefix (e.g. \\HR\\)"
              value={pathPrefix}
              onChange={(event) => setPathPrefix(event.target.value)}
            />
          </div>
          <div className="mb-2 flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveBack(setItemCursor, setItemHistory)}
              disabled={itemHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveCursor(itemNext, itemCursor, setItemCursor, setItemHistory)}
              disabled={!itemNext}
            >
              Next
            </button>
          </div>
          <ul className="max-h-[420px] space-y-2 overflow-auto">
            {items.map((item) => (
              <li key={item.id} className="rounded-lg border border-slate-300 px-2 py-2 text-xs dark:border-slate-700">
                <div className="font-mono">{item.path}</div>
                <div className="text-slate-500">{item.is_dir ? "directory" : "file"}</div>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="workspace-section">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <h2 className="text-lg font-semibold">Run-Scoped Search</h2>
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <input
                className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Query"
                value={globalQuery}
                onChange={(event) => setGlobalQuery(event.target.value)}
              />
            </div>
            <div>
              <input
                className="w-24 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder=".ext"
                value={globalExt}
                onChange={(event) => setGlobalExt(event.target.value)}
              />
            </div>
            <div className="flex items-center gap-2">
              <input
                className="w-32 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Save as..."
                value={savedQueryLabel}
                onChange={(event) => setSavedQueryLabel(event.target.value)}
              />
              <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onClick={saveCurrentQuery}>
                Save
              </button>
            </div>
          </div>
        </div>

        {savedQueries.length > 0 ? (
          <div className="mb-3 flex flex-wrap gap-2">
            {savedQueries.map((saved) => (
              <div key={saved.id} className="flex items-center gap-1 rounded-full bg-slate-100 px-2 py-1 text-xs dark:bg-slate-800">
                <button
                  className="font-semibold"
                  onClick={() => {
                    setGlobalQuery(saved.q);
                    setGlobalExt(saved.ext);
                  }}
                >
                  {saved.label}
                </button>
                <button className="text-slate-500" onClick={() => removeSavedQuery(saved.id)}>
                  x
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <div className="mb-2 flex items-center gap-2">
          <button
            className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
            onClick={() => moveBack(setGlobalCursor, setGlobalHistory)}
            disabled={globalHistory.length === 0}
          >
            Prev
          </button>
          <button
            className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
            onClick={() => moveCursor(globalNext, globalCursor, setGlobalCursor, setGlobalHistory)}
            disabled={!globalNext}
          >
            Next
          </button>
        </div>

        <ul className="max-h-[280px] space-y-2 overflow-auto">
          {globalItems.map((item) => (
            <li key={item.id} className="rounded-lg border border-slate-300 px-2 py-2 text-xs dark:border-slate-700">
              <div className="font-mono">{item.path}</div>
              <div className="text-slate-500">resource_id: {item.resource_id ?? "-"}</div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
