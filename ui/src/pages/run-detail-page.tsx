import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiFetch, apiFetchAllPages } from "@/lib/api";

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
type RunDetailTab = "overview" | "diff" | "explore" | "search";

const RUN_STATUS_COLORS: Record<string, string> = {
  PENDING_UPLOAD: "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-200",
  UPLOADED: "bg-amber-200 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  INGESTING: "bg-sky-200 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  COMPLETE: "bg-emerald-200 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  FAILED: "bg-rose-200 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
};

const RUN_DETAIL_TAB_COPY: Record<RunDetailTab, { label: string; description: string }> = {
  overview: {
    label: "Overview",
    description: "Status, baseline context, and next actions for this collector run.",
  },
  diff: {
    label: "Diff",
    description: "Compare this run to the chosen baseline and review churn.",
  },
  explore: {
    label: "Explore",
    description: "Browse endpoints, shares, and items inside this run.",
  },
  search: {
    label: "Search",
    description: "Run-scoped item search with reusable saved queries.",
  },
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
  const [activeTab, setActiveTab] = useState<RunDetailTab>("overview");

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
    apiFetchAllPages<RunCompareOption>((cursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (cursor) query.set("cursor", cursor);
      return `/projects/${projectId}/runs?${query.toString()}`;
    })
      .then((data) => setProjectRuns(data))
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
    setActiveTab("overview");
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
        setSelectedEndpoint((current) => {
          if (current && rows.some((endpoint) => endpoint.id === current)) {
            return current;
          }
          return rows[0]?.id || null;
        });
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, endpointSearch, endpointCursor]);

  useEffect(() => {
    if (!projectId || !runId || !selectedEndpoint) {
      setResources([]);
      setSelectedResource(null);
      return;
    }

    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources`)
      .then((data) => {
        const rows = (data?.items || []) as Resource[];
        setResources(rows);
        setSelectedResource((current) => {
          if (current && rows.some((resource) => resource.id === current)) {
            return current;
          }
          return rows[0]?.id || null;
        });
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, selectedEndpoint]);

  useEffect(() => {
    setItemCursor(null);
    setItemHistory([]);
  }, [selectedResource, itemSearch, pathPrefix, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || !selectedResource) {
      setItems([]);
      setItemNext(null);
      return;
    }

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

  function formatScopeValue(value: unknown): string {
    if (Array.isArray(value)) return value.join(", ");
    if (value && typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  const targetScopeEntries = Object.entries(run?.target_scope || {}).filter(([, value]) => {
    if (Array.isArray(value)) return value.length > 0;
    return value !== null && value !== undefined && value !== "";
  });
  const summaryChips = [
    { label: "Endpoints", value: run?.summary?.endpoints || 0, tone: "bg-slate-100 dark:bg-slate-800" },
    { label: "Shares", value: run?.summary?.resources || 0, tone: "bg-slate-100 dark:bg-slate-800" },
    { label: "Items", value: run?.summary?.items || 0, tone: "bg-slate-100 dark:bg-slate-800" },
    {
      label: "Errors",
      value: run?.summary?.errors || 0,
      tone: (run?.summary?.errors || 0) > 0 ? "bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-200" : "bg-slate-100 dark:bg-slate-800",
    },
  ];
  const activeDiffSummary = runDiff?.baseline_run ? runDiff.summary : null;

  return (
    <section className="workspace">
      <div className="workspace-header gap-4">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_360px]">
          <div className="rounded-[28px] border border-slate-200 bg-[linear-gradient(135deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-5 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(135deg,rgba(15,23,42,0.96),rgba(15,23,42,0.8))]">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Run Explorer</p>
            <h1 className="mt-2 text-3xl font-bold tracking-tight">{run?.name || "Run Explorer"}</h1>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Run ID: {runId}</p>
            {run?.description ? <p className="mt-3 max-w-3xl text-sm text-slate-600 dark:text-slate-300">{run.description}</p> : null}
            <div className="mt-4 flex flex-wrap gap-2">
              <Link
                className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                to="/projects"
              >
                Open Dashboard
              </Link>
              {projectId ? (
                <Link
                  className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  to={`/projects/${projectId}/inventory`}
                >
                  Project Inventory
                </Link>
              ) : null}
            </div>
            {run ? (
              <div className="mt-4 flex flex-wrap gap-2">
                {summaryChips.map((chip) => (
                  <span className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] ${chip.tone}`} key={chip.label}>
                    {chip.label}: {chip.value.toLocaleString()}
                  </span>
                ))}
              </div>
            ) : null}
          </div>

          <div className="rounded-[28px] border border-slate-200 bg-white/90 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950/70">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Run Status</p>
            {run ? (
              <>
                <div className="mt-2 flex items-center gap-3">
                  <span className={`rounded-full px-3 py-1 text-xs font-semibold ${RUN_STATUS_COLORS[run.status] || "bg-slate-200 text-slate-900"}`}>
                    {run.status}
                  </span>
                  <span className="text-sm text-slate-500">Created {new Date(run.created_at).toLocaleString()}</span>
                </div>
                <p className="mt-4 text-sm text-slate-600 dark:text-slate-300">{RUN_DETAIL_TAB_COPY[activeTab].description}</p>
                <div className="mt-4 space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Baseline</p>
                    <p className="mt-1 text-sm font-semibold">
                      {runDiff?.baseline_run ? runDiff.baseline_run.name : "Nearest previous complete run not available yet"}
                    </p>
                  </div>
                  {activeDiffSummary ? (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">New Shares</p>
                        <p className="mt-1 text-sm font-semibold">{activeDiffSummary.new_shares.toLocaleString()}</p>
                      </div>
                      <div>
                        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Changed Shares</p>
                        <p className="mt-1 text-sm font-semibold">{activeDiffSummary.changed_shares.toLocaleString()}</p>
                      </div>
                    </div>
                  ) : null}
                </div>
              </>
            ) : (
              <p className="mt-2 text-sm text-slate-500">Loading run details.</p>
            )}
          </div>
        </div>

        <div className="grid gap-3 lg:grid-cols-4">
          {(["overview", "diff", "explore", "search"] as RunDetailTab[]).map((tab) => (
            <button
              className={`rounded-3xl border p-4 text-left transition ${
                activeTab === tab
                  ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                  : "border-slate-300 bg-white/80 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-950/40 dark:hover:bg-slate-900/80"
              }`}
              key={tab}
              onClick={() => setActiveTab(tab)}
              type="button"
            >
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{RUN_DETAIL_TAB_COPY[tab].label}</p>
              <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{RUN_DETAIL_TAB_COPY[tab].description}</p>
            </button>
          ))}
        </div>

        {error ? (
          <p className="rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200">{error}</p>
        ) : null}
      </div>

      {activeTab === "overview" ? (
        <div className="workspace-section space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_360px]">
            <div className="workspace-card space-y-4">
              <div>
                <h2 className="text-lg font-semibold">Run Summary</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  Use the tabs below to move between diff review, hierarchical exploration, and targeted search without keeping every surface visible at once.
                </p>
              </div>
              {targetScopeEntries.length > 0 ? (
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Target Scope</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {targetScopeEntries.map(([key, value]) => (
                      <span className="rounded-full bg-slate-100 px-3 py-1 text-xs dark:bg-slate-800" key={key}>
                        {key}: {formatScopeValue(value)}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <button
                  className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                  onClick={() => setActiveTab("diff")}
                  type="button"
                >
                  Review Diff
                </button>
                <button
                  className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  onClick={() => setActiveTab("explore")}
                  type="button"
                >
                  Explore Run
                </button>
                <button
                  className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  onClick={() => setActiveTab("search")}
                  type="button"
                >
                  Search Items
                </button>
              </div>
            </div>

            <div className="workspace-card space-y-4">
              <div>
                <h2 className="text-lg font-semibold">Diff Snapshot</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  A concise baseline summary so you can decide whether to dive into detailed churn review.
                </p>
              </div>
              {diffLoading ? <p className="text-sm text-slate-500">Loading baseline comparison.</p> : null}
              {diffError ? <p className="rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200">{diffError}</p> : null}
              {runDiff?.baseline_run ? (
                <div className="space-y-3">
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Baseline Run</p>
                    <p className="mt-1 text-sm font-semibold">{runDiff.baseline_run.name}</p>
                    <p className="mt-1 text-xs text-slate-500">
                      {runDiff.baseline_run.created_at ? new Date(runDiff.baseline_run.created_at).toLocaleString() : "unknown time"}
                    </p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">New Shares</p>
                      <p className="mt-1 text-xl font-semibold">{runDiff.summary.new_shares.toLocaleString()}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Added Items</p>
                      <p className="mt-1 text-xl font-semibold">{runDiff.summary.added_items.toLocaleString()}</p>
                    </div>
                  </div>
                </div>
              ) : (
                !diffLoading && <p className="text-sm text-slate-500">No earlier complete run is available for comparison yet.</p>
              )}
            </div>
          </div>
        </div>
      ) : null}

      {activeTab === "diff" ? (
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
                  className="mt-1 w-full rounded-2xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
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

            {diffLoading ? <p className="mt-3 text-sm text-slate-500">Loading run diff.</p> : null}
            {diffError ? <p className="mt-3 rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200">{diffError}</p> : null}

            {runDiff && !diffLoading ? (
              runDiff.baseline_run ? (
                <div className="mt-4 space-y-4">
                  <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                    <div className="rounded-2xl border border-slate-300 p-3 text-sm dark:border-slate-700">
                      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Baseline</p>
                      <p className="mt-1 font-semibold">{runDiff.baseline_run.name}</p>
                      <p className="mt-1 text-xs text-slate-500">
                        {runDiff.baseline_run.status} •{" "}
                        {runDiff.baseline_run.created_at ? new Date(runDiff.baseline_run.created_at).toLocaleString() : "unknown time"}
                      </p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 p-3 text-sm dark:border-slate-700">
                      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Current</p>
                      <p className="mt-1 font-semibold">{runDiff.current_run.name}</p>
                      <p className="mt-1 text-xs text-slate-500">
                        {runDiff.current_run.status} •{" "}
                        {runDiff.current_run.created_at ? new Date(runDiff.current_run.created_at).toLocaleString() : "unknown time"}
                      </p>
                    </div>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                    <div className="rounded-2xl border border-slate-300 p-3 dark:border-slate-700">
                      <p className="text-[11px] uppercase tracking-wide text-slate-500">New Shares</p>
                      <p className="mt-1 text-2xl font-semibold">{runDiff.summary.new_shares}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 p-3 dark:border-slate-700">
                      <p className="text-[11px] uppercase tracking-wide text-slate-500">Disappeared Shares</p>
                      <p className="mt-1 text-2xl font-semibold">{runDiff.summary.disappeared_shares}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 p-3 dark:border-slate-700">
                      <p className="text-[11px] uppercase tracking-wide text-slate-500">Changed Shares</p>
                      <p className="mt-1 text-2xl font-semibold">{runDiff.summary.changed_shares}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 p-3 dark:border-slate-700">
                      <p className="text-[11px] uppercase tracking-wide text-slate-500">Added Items</p>
                      <p className="mt-1 text-2xl font-semibold">{runDiff.summary.added_items}</p>
                    </div>
                    <div className="rounded-2xl border border-slate-300 p-3 dark:border-slate-700">
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
                    <li className="rounded-2xl border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
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
                    <li className="rounded-2xl border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
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
                    <li className="rounded-2xl border border-slate-300 p-3 text-xs dark:border-slate-700" key={`${share.endpoint_key}:${share.share_name}`}>
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
      ) : null}

      {activeTab === "explore" ? (
        <div className="workspace-section grid gap-4 md:grid-cols-3">
          <div className="workspace-card">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <h2 className="text-lg font-semibold">Endpoints</h2>
                <p className="mt-1 text-xs text-slate-500">Choose a host to load its shares.</p>
              </div>
              <input
                className="w-44 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search endpoint"
                value={endpointSearch}
                onChange={(event) => setEndpointSearch(event.target.value)}
              />
            </div>
            <div className="mb-3 flex items-center gap-2">
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveBack(setEndpointCursor, setEndpointHistory)}
                disabled={endpointHistory.length === 0}
                type="button"
              >
                Prev
              </button>
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveCursor(endpointNext, endpointCursor, setEndpointCursor, setEndpointHistory)}
                disabled={!endpointNext}
                type="button"
              >
                Next
              </button>
            </div>
            {endpoints.length === 0 ? <p className="text-sm text-slate-500">No endpoints match this run search.</p> : null}
            <ul className="space-y-2">
              {endpoints.map((endpoint) => (
                <li key={endpoint.id}>
                  <button
                    className={`w-full rounded-2xl border px-3 py-3 text-left text-xs ${
                      selectedEndpoint === endpoint.id
                        ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                        : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    }`}
                    onClick={() => setSelectedEndpoint(endpoint.id)}
                    type="button"
                  >
                    <div className="font-semibold">{endpoint.endpoint_key}</div>
                    <div className="mt-1 text-slate-500">
                      {(endpoint.hostname || endpoint.ip || "-") + (endpoint.smb_signing ? ` • signing:${endpoint.smb_signing}` : "")}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="workspace-card">
            <div className="mb-3">
              <h2 className="text-lg font-semibold">Shares</h2>
              <p className="mt-1 text-xs text-slate-500">Select a share to inspect items in that branch.</p>
            </div>
            {resources.length === 0 ? <p className="text-sm text-slate-500">No shares are available for the selected endpoint.</p> : null}
            <ul className="space-y-2">
              {resources.map((resource) => (
                <li key={resource.id}>
                  <button
                    className={`w-full rounded-2xl border px-3 py-3 text-left text-xs ${
                      selectedResource === resource.id
                        ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                        : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    }`}
                    onClick={() => setSelectedResource(resource.id)}
                    type="button"
                  >
                    <span className="block font-semibold">{resource.name}</span>
                    <span className="mt-1 block text-slate-500">
                      {resource.share_type.toUpperCase()} • {resource.access_level}
                    </span>
                    {resource.remark ? <span className="mt-1 block text-slate-500">{resource.remark}</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="workspace-card">
            <div className="mb-3 grid gap-2">
              <div>
                <h2 className="text-lg font-semibold">Items</h2>
                <p className="mt-1 text-xs text-slate-500">Filter the selected share by name or path prefix.</p>
              </div>
              <input
                className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search name"
                value={itemSearch}
                onChange={(event) => setItemSearch(event.target.value)}
              />
              <input
                className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Path prefix (e.g. \\HR\\)"
                value={pathPrefix}
                onChange={(event) => setPathPrefix(event.target.value)}
              />
            </div>
            <div className="mb-3 flex items-center gap-2">
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveBack(setItemCursor, setItemHistory)}
                disabled={itemHistory.length === 0}
                type="button"
              >
                Prev
              </button>
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveCursor(itemNext, itemCursor, setItemCursor, setItemHistory)}
                disabled={!itemNext}
                type="button"
              >
                Next
              </button>
            </div>
            {items.length === 0 ? <p className="text-sm text-slate-500">No items match the current share filters.</p> : null}
            <ul className="max-h-[420px] space-y-2 overflow-auto">
              {items.map((item) => (
                <li key={item.id} className="rounded-2xl border border-slate-300 px-3 py-3 text-xs dark:border-slate-700">
                  <div className="font-mono">{item.path}</div>
                  <div className="mt-1 text-slate-500">{item.is_dir ? "directory" : "file"}</div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {activeTab === "search" ? (
        <div className="workspace-section space-y-4">
          <div className="workspace-card">
            <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Run-Scoped Search</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  Search items across the full run and save commonly reused query/ext combinations.
                </p>
              </div>
              <div className="flex flex-wrap items-end gap-2">
                <div>
                  <input
                    className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder="Query"
                    value={globalQuery}
                    onChange={(event) => setGlobalQuery(event.target.value)}
                  />
                </div>
                <div>
                  <input
                    className="w-24 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder=".ext"
                    value={globalExt}
                    onChange={(event) => setGlobalExt(event.target.value)}
                  />
                </div>
                <div className="flex items-center gap-2">
                  <input
                    className="w-36 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder="Save as..."
                    value={savedQueryLabel}
                    onChange={(event) => setSavedQueryLabel(event.target.value)}
                  />
                  <button
                    className="rounded-2xl bg-slate-900 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                    onClick={saveCurrentQuery}
                    type="button"
                  >
                    Save
                  </button>
                </div>
              </div>
            </div>

            {savedQueries.length > 0 ? (
              <div className="mb-4 flex flex-wrap gap-2">
                {savedQueries.map((saved) => (
                  <div key={saved.id} className="flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-xs dark:bg-slate-800">
                    <button
                      className="font-semibold"
                      onClick={() => {
                        setGlobalQuery(saved.q);
                        setGlobalExt(saved.ext);
                      }}
                      type="button"
                    >
                      {saved.label}
                    </button>
                    <button className="text-slate-500" onClick={() => removeSavedQuery(saved.id)} type="button">
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="mb-3 flex items-center gap-2">
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveBack(setGlobalCursor, setGlobalHistory)}
                disabled={globalHistory.length === 0}
                type="button"
              >
                Prev
              </button>
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveCursor(globalNext, globalCursor, setGlobalCursor, setGlobalHistory)}
                disabled={!globalNext}
                type="button"
              >
                Next
              </button>
            </div>

            {globalItems.length === 0 ? <p className="text-sm text-slate-500">No run-scoped search results match the current query.</p> : null}
            <ul className="max-h-[360px] space-y-2 overflow-auto">
              {globalItems.map((item) => (
                <li key={item.id} className="rounded-2xl border border-slate-300 px-3 py-3 text-xs dark:border-slate-700">
                  <div className="font-mono">{item.path}</div>
                  <div className="mt-1 text-slate-500">resource_id: {item.resource_id ?? "-"}</div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </section>
  );
}
