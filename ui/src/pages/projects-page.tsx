import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Dialog } from "@/components/dialog";
import { apiFetch } from "@/lib/api";
import { useDashboardWorkspace } from "@/lib/dashboard-workspace";

type RunProgress = {
  line_offset?: number;
  last_error?: string;
  attempt_count?: number;
  next_retry_at?: string;
  [key: string]: unknown;
};

type Run = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  artifact_size: number | null;
  ingest_progress: RunProgress;
  summary: { endpoints?: number; resources?: number; items?: number; errors?: number };
};

type ProjectStats = {
  runs_total: number;
  runs_complete: number;
  runs_ingesting: number;
  scope_runs: number;
  endpoints: number;
  shares: number;
  items: number;
  files: number;
  directories: number;
  file_types: number;
  unique_hosts: number;
  latest_run_at: string | null;
};

type ExtensionStat = {
  ext: string;
  count: number;
};

const RUN_STATUS_COLORS: Record<string, string> = {
  PENDING_UPLOAD: "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-200",
  UPLOADED: "bg-amber-200 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  INGESTING: "bg-sky-200 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  COMPLETE: "bg-emerald-200 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  FAILED: "bg-rose-200 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
};

function parseLineOffset(progress: RunProgress | null | undefined): number {
  const raw = progress?.line_offset;
  const parsed = typeof raw === "number" ? raw : Number.parseInt(String(raw ?? "0"), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function describeRunCardStatus(run: Run) {
  const lineOffset = parseLineOffset(run.ingest_progress);
  const lastError = typeof run.ingest_progress?.last_error === "string" ? run.ingest_progress.last_error : null;
  const attemptCount = Math.max(0, Number(run.ingest_progress?.attempt_count || 0));
  const nextRetryAt = typeof run.ingest_progress?.next_retry_at === "string" ? run.ingest_progress.next_retry_at : null;
  const parsedRetryAt = nextRetryAt ? new Date(nextRetryAt) : null;
  const retryAtLabel = parsedRetryAt && !Number.isNaN(parsedRetryAt.getTime()) ? parsedRetryAt.toLocaleString() : null;

  if (run.status === "UPLOADED") {
    if (attemptCount > 0 || nextRetryAt || lastError) {
      return {
        title: `Ingest retry attempt ${attemptCount + 1} scheduled`,
        detail: `${lastError || "The previous worker attempt did not complete."} ${retryAtLabel ? `Next retry: ${retryAtLabel}.` : "The recovery worker will retry when due."}`,
        tone: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200",
        progressTone: "bg-amber-500",
        progressWidth: "44%",
        pulse: true,
      };
    }
    return {
      title: "Artifact uploaded, waiting for worker pickup",
      detail: "The file is stored and queued. No worker checkpoint has been recorded yet.",
      tone: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200",
      progressTone: "bg-amber-500",
      progressWidth: "32%",
      pulse: true,
    };
  }

  if (run.status === "INGESTING") {
    return {
      title: "Ingest in progress",
      detail:
        lineOffset > 0
          ? `Worker checkpoint confirmed at line ${lineOffset.toLocaleString()}. Counts below reflect committed records.`
          : "Worker started, but no checkpoint line has been written yet.",
      tone: "border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-900/40 dark:bg-sky-900/20 dark:text-sky-200",
      progressTone: "bg-sky-500",
      progressWidth: "72%",
      pulse: true,
    };
  }

  if (run.status === "FAILED") {
    return {
      title: "Ingest failed",
      detail: lastError || "The worker reported a failure before the run completed.",
      tone: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-900/40 dark:bg-rose-900/20 dark:text-rose-200",
      progressTone: "bg-rose-500",
      progressWidth: "100%",
      pulse: false,
    };
  }

  return null;
}

function StatTile({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-950">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-tight">{value}</p>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{detail}</p>
    </div>
  );
}

function RunMetric({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900/80">
      <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">{label}</p>
      <p className="mt-1 text-lg font-semibold">{(value || 0).toLocaleString()}</p>
    </div>
  );
}

export function ProjectsPage() {
  const { canCreateProject, projectCount, projectLoadError, projectsReady, selectedProject, selectedProjectName } = useDashboardWorkspace();
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [runs, setRuns] = useState<Run[]>([]);
  const [runsProjectId, setRunsProjectId] = useState<string | null>(null);
  const [runSearch, setRunSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [projectStats, setProjectStats] = useState<ProjectStats | null>(null);
  const [topExtensions, setTopExtensions] = useState<ExtensionStat[]>([]);
  const [insightsProjectId, setInsightsProjectId] = useState<string | null>(null);
  const [loadingStats, setLoadingStats] = useState(false);
  const [runToDelete, setRunToDelete] = useState<Run | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [refreshWarning, setRefreshWarning] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [roleProjectId, setRoleProjectId] = useState<string | null>(null);
  const selectedProjectRef = useRef(selectedProject);
  selectedProjectRef.current = selectedProject;

  async function loadRuns(projectId: string, pageCursor: string | null, signal?: AbortSignal) {
    if (signal?.aborted || selectedProjectRef.current !== projectId) return;
    const query = new URLSearchParams({ limit: "50" });
    if (pageCursor) query.set("cursor", pageCursor);
    const data = await apiFetch(`/projects/${projectId}/runs?${query.toString()}`, { signal });
    if (signal?.aborted || selectedProjectRef.current !== projectId) return;
    setRuns((data?.items || []) as Run[]);
    setNextCursor((data?.next_cursor as string | null) || null);
    setRunsProjectId(projectId);
  }

  async function loadProjectInsights(projectId: string, signal?: AbortSignal) {
    if (signal?.aborted || selectedProjectRef.current !== projectId) return;
    setLoadingStats(true);
    try {
      const [statsData, extData] = await Promise.all([
        apiFetch(`/projects/${projectId}/inventory/stats`, { signal }),
        apiFetch(`/projects/${projectId}/inventory/extensions?limit=8`, { signal }),
      ]);
      if (signal?.aborted || selectedProjectRef.current !== projectId) return;
      setProjectStats((statsData || null) as ProjectStats | null);
      setTopExtensions(((extData?.items as ExtensionStat[]) || []).filter((entry) => !!entry.ext));
      setInsightsProjectId(projectId);
    } finally {
      if (!signal?.aborted && selectedProjectRef.current === projectId) setLoadingStats(false);
    }
  }

  async function refreshProjectSnapshot(projectId: string, pageCursor: string | null, signal?: AbortSignal) {
    if (signal?.aborted || selectedProjectRef.current !== projectId) return;
    const query = new URLSearchParams({ limit: "50" });
    if (pageCursor) query.set("cursor", pageCursor);
    const [runsData, statsData, extData] = await Promise.all([
      apiFetch(`/projects/${projectId}/runs?${query.toString()}`, { signal }),
      apiFetch(`/projects/${projectId}/inventory/stats`, { signal }),
      apiFetch(`/projects/${projectId}/inventory/extensions?limit=8`, { signal }),
    ]);
    if (signal?.aborted || selectedProjectRef.current !== projectId) return;
    setRuns((runsData?.items || []) as Run[]);
    setNextCursor((runsData?.next_cursor as string | null) || null);
    setRunsProjectId(projectId);
    setProjectStats((statsData || null) as ProjectStats | null);
    setTopExtensions(((extData?.items as ExtensionStat[]) || []).filter((entry) => !!entry.ext));
    setInsightsProjectId(projectId);
  }

  useEffect(() => {
    if (!selectedProject) return;
    setCursor(null);
    setCursorHistory([]);
    setInfo(null);
    setError(null);
    setRefreshWarning(null);
    setRuns([]);
    setRunsProjectId(null);
    setNextCursor(null);
    setProjectStats(null);
    setTopExtensions([]);
    setInsightsProjectId(null);
    setProjectRole(null);
    setRoleProjectId(null);
    setRunToDelete(null);
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    const controller = new AbortController();
    setError(null);
    setRefreshWarning(null);
    loadRuns(selectedProject, cursor, controller.signal).catch((err) => {
      if (!controller.signal.aborted && selectedProjectRef.current === selectedProject && !isAbortError(err)) {
        setError(err.message);
      }
    });
    loadProjectInsights(selectedProject, controller.signal).catch((err) => {
      if (!controller.signal.aborted && selectedProjectRef.current === selectedProject && !isAbortError(err)) {
        setError(err.message);
      }
    });
    return () => controller.abort();
  }, [selectedProject, cursor, reloadNonce]);

  useEffect(() => {
    if (!selectedProject) {
      setProjectRole(null);
      setRoleProjectId(null);
      return;
    }
    const controller = new AbortController();
    setProjectRole(null);
    setRoleProjectId(null);
    apiFetch(`/projects/${selectedProject}/my-role`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted && selectedProjectRef.current === selectedProject) {
          setProjectRole((data?.role as string) || null);
          setRoleProjectId(selectedProject);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted && selectedProjectRef.current === selectedProject && !isAbortError(err)) {
          setProjectRole(null);
        }
      });
    return () => controller.abort();
  }, [selectedProject, reloadNonce]);

  const scopedRuns = runsProjectId === selectedProject ? runs : [];
  const scopedProjectStats = insightsProjectId === selectedProject ? projectStats : null;
  const scopedTopExtensions = insightsProjectId === selectedProject ? topExtensions : [];
  const scopedProjectRole = roleProjectId === selectedProject ? projectRole : null;
  const shouldPoll = scopedRuns.some((run) => run.status === "UPLOADED" || run.status === "INGESTING");

  useEffect(() => {
    if (!selectedProject || !shouldPoll) return;

    let stopped = false;
    let timer: number | null = null;
    let refreshController: AbortController | null = null;

    const poll = async () => {
      const tickController = new AbortController();
      refreshController = tickController;
      const results = await Promise.allSettled([
        refreshProjectSnapshot(selectedProject, cursor, tickController.signal),
      ]);
      if (stopped || tickController.signal.aborted || selectedProjectRef.current !== selectedProject) return;
      const failed = results.find((result) => result.status === "rejected") as PromiseRejectedResult | undefined;
      setRefreshWarning(
        failed
          ? `Live refresh is delayed; showing the last confirmed project state. ${failed.reason instanceof Error ? failed.reason.message : "Retry when the API is available."}`
          : null,
      );
      timer = window.setTimeout(() => {
        void poll();
      }, 4000);
    };

    timer = window.setTimeout(() => {
      void poll();
    }, 4000);
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      refreshController?.abort();
    };
  }, [cursor, selectedProject, shouldPoll]);

  const visibleRuns = useMemo(() => {
    return scopedRuns.filter((run) => {
      const statusOk = statusFilter === "all" || run.status === statusFilter;
      const search = runSearch.trim().toLowerCase();
      const searchOk =
        search === "" ||
        run.name.toLowerCase().includes(search) ||
        (run.description || "").toLowerCase().includes(search) ||
        run.id.toLowerCase().includes(search);
      return statusOk && searchOk;
    });
  }, [scopedRuns, runSearch, statusFilter]);

  const latestRun = scopedRuns.length > 0 ? scopedRuns[0] : null;
  const activeRunCount = scopedRuns.filter((run) => run.status === "UPLOADED" || run.status === "INGESTING").length;

  function moveNext() {
    if (!nextCursor) return;
    setCursorHistory((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }

  function movePrev() {
    setCursorHistory((prev) => {
      if (prev.length === 0) return prev;
      const copy = [...prev];
      const previousCursor = copy.pop() ?? null;
      setCursor(previousCursor);
      return copy;
    });
  }

  async function deleteRun() {
    if (!selectedProject || !runToDelete) return;
    const targetProjectId = selectedProject;
    const targetRun = runToDelete;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/projects/${targetProjectId}/runs/${targetRun.id}`, { method: "DELETE" });
      if (selectedProjectRef.current !== targetProjectId) return;
      setInfo(`Run ${targetRun.id} deleted.`);
      setRunToDelete(null);
      await loadRuns(targetProjectId, cursor);
      if (selectedProjectRef.current !== targetProjectId) return;
      await loadProjectInsights(targetProjectId);
    } catch (err) {
      if (selectedProjectRef.current === targetProjectId) {
        setError(err instanceof Error ? err.message : "Run deletion failed");
      }
    }
  }

  const canImport = scopedProjectRole === "operator" || scopedProjectRole === "admin";
  const canDeleteRuns = scopedProjectRole === "admin";
  const latestRunAtText = scopedProjectStats?.latest_run_at ? new Date(scopedProjectStats.latest_run_at).toLocaleString() : "No completed runs yet";
  const visibleError = error || projectLoadError;
  const roleLabel = scopedProjectRole ? scopedProjectRole.toUpperCase() : "ROLE UNKNOWN";

  function formatCount(value: number | null | undefined): string {
    if (value === null || value === undefined) return loadingStats ? "..." : "0";
    return value.toLocaleString();
  }

  function formatFileSize(value: number | null): string {
    if (!value) return "No artifact";
    if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_360px]">
          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Project overview</p>
                <h1 className="mt-1 text-2xl font-semibold tracking-tight">{selectedProjectName || "Select a project"}</h1>
                <div className="mt-3 flex flex-wrap gap-2">
                  <span className="rounded-full border border-slate-300 bg-white/70 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] dark:border-slate-700 dark:bg-slate-950/40">
                    {roleLabel}
                  </span>
                  <span className="rounded-full border border-slate-300 bg-white/70 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] dark:border-slate-700 dark:bg-slate-950/40">
                    {activeRunCount.toLocaleString()} active ingest{activeRunCount === 1 ? "" : "s"}
                  </span>
                  <span className="rounded-full border border-slate-300 bg-white/70 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] dark:border-slate-700 dark:bg-slate-950/40">
                    {projectCount.toLocaleString()} project{projectCount === 1 ? "" : "s"}
                  </span>
                  {latestRun ? (
                    <span className={`rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${RUN_STATUS_COLORS[latestRun.status] || "bg-slate-200 text-slate-900"}`}>
                      Latest run: {latestRun.status}
                    </span>
                  ) : null}
                </div>
                <p className="mt-3 max-w-2xl text-sm text-slate-600 dark:text-slate-300">
                  Work from one project at a time: switch context in the top bar, jump straight into inventory, or monitor the newest ingest without leaving the dashboard.
                </p>
              </div>
            </div>

            {selectedProject ? (
              <div className="mt-4 flex flex-wrap gap-2">
                <Link
                  className="rounded-2xl bg-emerald-600 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-emerald-500"
                  to={`/projects/${selectedProject}/inventory`}
                >
                  Browse Inventory
                </Link>
                {canImport ? (
                  <Link
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    to={`/projects/${selectedProject}/import`}
                  >
                    Import Scan
                  </Link>
                ) : (
                  <span className="rounded-2xl border border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-slate-500 dark:border-slate-800 dark:text-slate-400">
                    Import requires operator or admin
                  </span>
                )}
                {latestRun ? (
                  <Link
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    to={`/projects/${selectedProject}/runs/${latestRun.id}`}
                  >
                    Open Latest Run
                  </Link>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Next action</p>
            <h2 className="mt-2 text-xl font-semibold">
              {!selectedProject ? "Create or select a project" : activeRunCount > 0 ? "Monitor the active ingest" : latestRun ? "Review the latest run" : "Import the first scan"}
            </h2>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
              {!selectedProject
                ? canCreateProject
                  ? "Use the top bar to create a workspace, then start ingesting collector output."
                  : projectsReady
                    ? "Ask a sysadmin to create a project before review can begin."
                    : "Loading available workspaces."
                : activeRunCount > 0
                  ? `Latest activity ${latestRunAtText}. Keep the run queue visible and follow progress as artifacts ingest.`
                  : latestRun
                    ? `Latest completed activity ${latestRunAtText}. Open the run diff or move straight into inventory review.`
                    : canImport
                      ? "No runs have been ingested yet. Create one now and land directly in the run explorer when upload completes."
                      : "No runs have been ingested yet. An operator or admin needs to upload the first artifact."}
            </p>

            <div className="mt-4 space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Latest Activity</p>
                  <p className="mt-1 text-sm font-semibold">{latestRun ? latestRun.name : "No runs yet"}</p>
                </div>
                {latestRun ? (
                  <span className={`rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${RUN_STATUS_COLORS[latestRun.status] || "bg-slate-200 text-slate-900"}`}>
                    {latestRun.status}
                  </span>
                ) : null}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Last update</p>
                  <p className="mt-1 text-sm font-semibold">{latestRunAtText}</p>
                </div>
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Visible runs</p>
                  <p className="mt-1 text-sm font-semibold">{visibleRuns.length.toLocaleString()}</p>
                </div>
              </div>
            </div>

            {selectedProject ? (
              <div className="mt-4 flex flex-wrap gap-2">
                {latestRun ? (
                  <Link
                    className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                    to={`/projects/${selectedProject}/runs/${latestRun.id}`}
                  >
                    Open Run Explorer
                  </Link>
                ) : null}
                <Link
                  className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  to={`/projects/${selectedProject}/inventory`}
                >
                  Open Inventory
                </Link>
                {canImport ? (
                  <Link
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    to={`/projects/${selectedProject}/import`}
                  >
                    Upload New Scan
                  </Link>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {visibleError || refreshWarning || info ? (
        <div className="workspace-section space-y-2">
          {visibleError ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200" role="alert">
              <span>{visibleError}</span>
              <button className="rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setReloadNonce((current) => current + 1)} type="button">
                Retry project data
              </button>
            </div>
          ) : null}
          {refreshWarning ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-amber-100 p-3 text-sm text-amber-800 dark:bg-amber-900/30 dark:text-amber-200" role="status">
              <span>{refreshWarning}</span>
              <button className="rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setReloadNonce((current) => current + 1)} type="button">
                Retry now
              </button>
            </div>
          ) : null}
          {info ? <p className="rounded-2xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      {selectedProject ? (
        <div className="workspace-section space-y-6">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Runs"
              value={formatCount(scopedProjectStats?.runs_total)}
              detail={`Scope: ${formatCount(scopedProjectStats?.scope_runs)} • Complete: ${formatCount(scopedProjectStats?.runs_complete)}`}
            />
            <StatTile
              label="Endpoints"
              value={formatCount(scopedProjectStats?.endpoints)}
              detail={`Unique hosts: ${formatCount(scopedProjectStats?.unique_hosts)} • Ingesting: ${formatCount(scopedProjectStats?.runs_ingesting)}`}
            />
            <StatTile
              label="Shares"
              value={formatCount(scopedProjectStats?.shares)}
              detail={`Files: ${formatCount(scopedProjectStats?.files)} • Directories: ${formatCount(scopedProjectStats?.directories)}`}
            />
            <StatTile
              label="Paths"
              value={formatCount(scopedProjectStats?.items)}
              detail={`File types: ${formatCount(scopedProjectStats?.file_types)} • Latest run: ${latestRunAtText}`}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
            <aside className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Run queue</p>
                <h2 className="mt-2 text-xl font-semibold">Filter and navigate</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                  Narrow the run list first, then jump directly to the run you want to inspect or delete.
                </p>
              </div>

              <div className="mt-5 space-y-3">
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                  Search runs
                  <input
                    className="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                    placeholder="Name, note, or run ID"
                    value={runSearch}
                    onChange={(event) => setRunSearch(event.target.value)}
                  />
                </label>

                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                  Status
                  <select
                    className="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                    value={statusFilter}
                    onChange={(event) => setStatusFilter(event.target.value)}
                  >
                    <option value="all">All statuses</option>
                    <option value="PENDING_UPLOAD">Pending upload</option>
                    <option value="UPLOADED">Uploaded</option>
                    <option value="INGESTING">Ingesting</option>
                    <option value="COMPLETE">Complete</option>
                    <option value="FAILED">Failed</option>
                  </select>
                </label>
              </div>

              <div className="mt-5 grid grid-cols-2 gap-2">
                <button
                  className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
                  onClick={movePrev}
                  disabled={cursorHistory.length === 0}
                >
                  Previous
                </button>
                <button
                  className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
                  onClick={moveNext}
                  disabled={!nextCursor}
                >
                  Next
                </button>
              </div>

              <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Queue Snapshot</p>
                <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                  <div>
                    <p className="text-sm font-semibold">{visibleRuns.length.toLocaleString()} visible runs</p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">Filtered from {scopedRuns.length.toLocaleString()} loaded runs.</p>
                  </div>
                  <div>
                    <p className="text-sm font-semibold">Auto-refresh while ingesting</p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">Polling every 4 seconds when uploads are active.</p>
                  </div>
                </div>
              </div>

              {scopedTopExtensions.length > 0 ? (
                <div className="mt-5">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Top file types</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {scopedTopExtensions.map((entry) => (
                      <span
                        key={entry.ext}
                        className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
                      >
                        {entry.ext} ({entry.count.toLocaleString()})
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
            </aside>

            <div className="space-y-3">
              {visibleRuns.length > 0 ? (
                visibleRuns.map((run) => {
                  const isLatest = latestRun?.id === run.id;
                  const runStatusNote = describeRunCardStatus(run);
                  const issueCount = run.summary?.errors || 0;
                  return (
                    <article
                      key={run.id}
                      className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition hover:border-slate-300 dark:border-slate-800 dark:bg-slate-950 dark:hover:border-slate-700"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-4">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className={`rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${RUN_STATUS_COLORS[run.status] || "bg-slate-200 text-slate-900"}`}>
                              {run.status}
                            </span>
                            {isLatest ? (
                              <span className="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300">
                                Latest
                              </span>
                            ) : null}
                          </div>
                          <h3 className="mt-3 text-xl font-semibold tracking-tight">{run.name}</h3>
                          <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{run.description || "No description provided."}</p>
                          <p className="mt-3 font-mono text-[11px] text-slate-500">{run.id}</p>
                        </div>

                        <div className="flex flex-wrap gap-2">
                          <Link
                            className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                            to={`/projects/${selectedProject}/runs/${run.id}`}
                          >
                            Open Run
                          </Link>
                          {canDeleteRuns ? (
                            <button
                              className="rounded-2xl border border-rose-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-rose-700 transition hover:bg-rose-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-950/40"
                              onClick={() => setRunToDelete(run)}
                            >
                              Delete
                            </button>
                          ) : null}
                        </div>
                      </div>

                      {runStatusNote ? (
                        <div className={`mt-4 rounded-2xl border px-4 py-3 ${runStatusNote.tone}`}>
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <p className="text-[11px] font-semibold uppercase tracking-[0.18em]">Pipeline status</p>
                              <p className="mt-1 text-sm font-semibold">{runStatusNote.title}</p>
                            </div>
                            {parseLineOffset(run.ingest_progress) > 0 ? (
                              <span className="rounded-full border border-current/20 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em]">
                                Line {parseLineOffset(run.ingest_progress).toLocaleString()}
                              </span>
                            ) : null}
                          </div>
                          <p className="mt-2 text-sm opacity-90">{runStatusNote.detail}</p>
                          <div className="mt-3 h-2 overflow-hidden rounded-full bg-white/50 dark:bg-slate-950/40">
                            <div
                              className={`h-full rounded-full ${runStatusNote.progressTone} ${runStatusNote.pulse ? "animate-pulse" : ""}`}
                              style={{ width: runStatusNote.progressWidth }}
                            />
                          </div>
                        </div>
                      ) : null}

                      {issueCount > 0 ? (
                        <div className="mt-3 rounded-2xl border border-amber-300 bg-amber-50 px-4 py-3 text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <p className="text-[11px] font-semibold uppercase tracking-[0.18em]">Recorded issues</p>
                              <p className="mt-1 text-sm font-semibold">
                                {issueCount.toLocaleString()} warning or error record{issueCount === 1 ? "" : "s"} need review.
                              </p>
                            </div>
                            <Link
                              className="rounded-2xl border border-amber-400/60 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-900/30"
                              to={`/projects/${selectedProject}/runs/${run.id}`}
                            >
                              Review Issues
                            </Link>
                          </div>
                        </div>
                      ) : null}

                      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
                        <RunMetric label="Endpoints" value={run.summary?.endpoints} />
                        <RunMetric label="Shares" value={run.summary?.resources} />
                        <RunMetric label="Items" value={run.summary?.items} />
                        <RunMetric label="Errors" value={run.summary?.errors} />
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900/80">
                          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">Created</p>
                          <p className="mt-1 text-sm font-semibold">{new Date(run.created_at).toLocaleString()}</p>
                        </div>
                        <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900/80">
                          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">Artifact</p>
                          <p className="mt-1 text-sm font-semibold">{formatFileSize(run.artifact_size)}</p>
                        </div>
                      </div>
                    </article>
                  );
                })
              ) : (
                <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center dark:border-slate-700 dark:bg-slate-950">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-500">No Runs In View</p>
                  <h3 className="mt-3 text-xl font-semibold">Adjust the filters or import a new scan.</h3>
                  <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                    The current search and status filter did not match any loaded runs for this workspace.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="workspace-section">
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center dark:border-slate-700 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-500">No Projects</p>
            <h2 className="mt-3 text-2xl font-semibold">Create or select a project to start reviewing scan data.</h2>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
              Once a workspace exists, the collector output, run queue, and inventory views all stay scoped to that project.
            </p>
          </div>
        </div>
      )}

      <Dialog
        open={!!runToDelete}
        title="Delete run"
        description={
          runToDelete
            ? `Delete ${runToDelete.name}. This removes the run record and all ingested entities created from its artifact.`
            : undefined
        }
        onClose={() => setRunToDelete(null)}
        footer={
          <>
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              onClick={() => setRunToDelete(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="rounded-2xl bg-rose-600 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-rose-500"
              onClick={() => {
                deleteRun().catch(() => undefined);
              }}
              type="button"
            >
              Delete run
            </button>
          </>
        }
      >
        <div className="space-y-3 text-sm text-slate-600 dark:text-slate-300">
          <p>Run IDs are immutable audit references. Delete runs only for bad uploads, duplicate ingest, or cleanup after verification.</p>
          {runToDelete ? <p className="font-mono text-xs text-slate-500">{runToDelete.id}</p> : null}
        </div>
      </Dialog>
    </section>
  );
}
