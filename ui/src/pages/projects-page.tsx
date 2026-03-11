import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "@/lib/api";
import { useDashboardWorkspace } from "@/lib/dashboard-workspace";

type Run = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  artifact_size: number | null;
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

function StatTile({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-3xl border border-slate-200 bg-white/90 p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950/60">
      <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-500">{label}</p>
      <p className="mt-3 text-3xl font-semibold tracking-tight">{value}</p>
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
  const { canCreateProject, projectLoadError, projects, projectsReady, selectedProject, selectedProjectName } = useDashboardWorkspace();
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [runs, setRuns] = useState<Run[]>([]);
  const [runSearch, setRunSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [projectStats, setProjectStats] = useState<ProjectStats | null>(null);
  const [topExtensions, setTopExtensions] = useState<ExtensionStat[]>([]);
  const [loadingStats, setLoadingStats] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadRuns(projectId: string, pageCursor: string | null) {
    const query = new URLSearchParams({ limit: "50" });
    if (pageCursor) query.set("cursor", pageCursor);
    const data = await apiFetch(`/projects/${projectId}/runs?${query.toString()}`);
    setRuns((data?.items || []) as Run[]);
    setNextCursor((data?.next_cursor as string | null) || null);
  }

  async function loadProjectInsights(projectId: string) {
    setLoadingStats(true);
    try {
      const [statsData, extData] = await Promise.all([
        apiFetch(`/projects/${projectId}/inventory/stats`),
        apiFetch(`/projects/${projectId}/inventory/extensions?limit=8`),
      ]);
      setProjectStats((statsData || null) as ProjectStats | null);
      setTopExtensions(((extData?.items as ExtensionStat[]) || []).filter((entry) => !!entry.ext));
    } finally {
      setLoadingStats(false);
    }
  }

  useEffect(() => {
    if (!selectedProject) return;
    setCursor(null);
    setCursorHistory([]);
    setInfo(null);
    setProjectStats(null);
    setTopExtensions([]);
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    loadRuns(selectedProject, cursor).catch((err) => setError(err.message));
    loadProjectInsights(selectedProject).catch((err) => setError(err.message));
  }, [selectedProject, cursor]);

  useEffect(() => {
    if (!selectedProject) {
      setProjectRole(null);
      return;
    }
    apiFetch(`/projects/${selectedProject}/my-role`)
      .then((data) => setProjectRole((data?.role as string) || null))
      .catch(() => setProjectRole(null));
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    const hasActiveRun = runs.some((run) => run.status === "UPLOADED" || run.status === "INGESTING");
    if (!hasActiveRun) return;

    const timer = window.setInterval(() => {
      loadRuns(selectedProject, cursor).catch(() => undefined);
      loadProjectInsights(selectedProject).catch(() => undefined);
    }, 8000);
    return () => window.clearInterval(timer);
  }, [selectedProject, runs, cursor]);

  const visibleRuns = useMemo(() => {
    return runs.filter((run) => {
      const statusOk = statusFilter === "all" || run.status === statusFilter;
      const search = runSearch.trim().toLowerCase();
      const searchOk =
        search === "" ||
        run.name.toLowerCase().includes(search) ||
        (run.description || "").toLowerCase().includes(search) ||
        run.id.toLowerCase().includes(search);
      return statusOk && searchOk;
    });
  }, [runs, runSearch, statusFilter]);

  const latestRun = runs.length > 0 ? runs[0] : null;
  const activeRunCount = runs.filter((run) => run.status === "UPLOADED" || run.status === "INGESTING").length;

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

  async function deleteRun(runId: string) {
    if (!selectedProject) return;
    if (!window.confirm("Delete this run? This removes all ingested entities for the run.")) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/projects/${selectedProject}/runs/${runId}`, { method: "DELETE" });
      setInfo(`Run ${runId} deleted.`);
      await loadRuns(selectedProject, cursor);
      await loadProjectInsights(selectedProject);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run deletion failed");
    }
  }

  const canImport = projectRole === "operator" || projectRole === "admin";
  const canDeleteRuns = projectRole === "admin";
  const latestRunAtText = projectStats?.latest_run_at ? new Date(projectStats.latest_run_at).toLocaleString() : "No completed runs yet";
  const visibleError = error || projectLoadError;

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
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_340px]">
          <div className="rounded-[28px] border border-slate-200 bg-[linear-gradient(135deg,rgba(255,255,255,0.98),rgba(226,232,240,0.9))] p-6 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(135deg,rgba(15,23,42,0.96),rgba(15,23,42,0.78))]">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Dashboard Workspace</p>
            <h1 className="mt-3 text-3xl font-bold tracking-tight">{selectedProjectName || "Dashboard"}</h1>
            <p className="mt-3 max-w-2xl text-sm text-slate-600 dark:text-slate-300">
              Keep project selection, run intake, and inventory review in one flow. Use the dashboard bar above to switch
              workspaces quickly, confirm coverage, then move directly into the run or inventory view you need.
            </p>
            {selectedProject ? (
              <div className="mt-5 flex flex-wrap gap-2">
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
                    Import Scan Requires Operator/Admin
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
            <div className="mt-6 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-white/60 bg-white/70 px-4 py-3 dark:border-slate-800 dark:bg-slate-950/50">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Latest Activity</p>
                <p className="mt-2 text-sm font-semibold">{latestRunAtText}</p>
              </div>
              <div className="rounded-2xl border border-white/60 bg-white/70 px-4 py-3 dark:border-slate-800 dark:bg-slate-950/50">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Active Ingests</p>
                <p className="mt-2 text-sm font-semibold">{activeRunCount.toLocaleString()}</p>
              </div>
              <div className="rounded-2xl border border-white/60 bg-white/70 px-4 py-3 dark:border-slate-800 dark:bg-slate-950/50">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Visible Runs</p>
                <p className="mt-2 text-sm font-semibold">{visibleRuns.length.toLocaleString()}</p>
              </div>
            </div>
          </div>

          <div className="rounded-[28px] border border-slate-200 bg-white/90 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950/70">
            <div>
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Workspace Summary</p>
                <h2 className="mt-2 text-xl font-semibold">{selectedProject ? "Current context" : "No project selected"}</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                  {selectedProject
                    ? "Project switching and creation now live in the top bar, so you can change focus without leaving the dashboard."
                    : canCreateProject
                      ? "Use the top bar to create your first project and start building the dashboard workspace."
                      : projectsReady
                        ? "No projects are available yet. Ask a sysadmin to create one."
                        : "Loading available dashboard workspaces."}
                </p>
              </div>
            </div>

            <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Projects Available</p>
              <p className="mt-2 text-sm font-semibold">{projectsReady ? projects.length.toLocaleString() : "Loading..."}</p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Switch or create projects from the dashboard controls in the top navbar.</p>
            </div>

            <div className="mt-4 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Access</p>
              <p className="mt-2 text-sm font-semibold">{projectRole || "Role unavailable"}</p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                Operators and admins can upload artifacts. Admins can also delete runs.
              </p>
            </div>
          </div>
        </div>
      </div>

      {visibleError || info ? (
        <div className="workspace-section space-y-2">
          {visibleError ? <p className="rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{visibleError}</p> : null}
          {info ? <p className="rounded-2xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      {selectedProject ? (
        <div className="workspace-section space-y-6">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Runs"
              value={formatCount(projectStats?.runs_total)}
              detail={`Scope: ${formatCount(projectStats?.scope_runs)} • Complete: ${formatCount(projectStats?.runs_complete)}`}
            />
            <StatTile
              label="Endpoints"
              value={formatCount(projectStats?.endpoints)}
              detail={`Unique hosts: ${formatCount(projectStats?.unique_hosts)} • Ingesting: ${formatCount(projectStats?.runs_ingesting)}`}
            />
            <StatTile
              label="Shares"
              value={formatCount(projectStats?.shares)}
              detail={`Files: ${formatCount(projectStats?.files)} • Directories: ${formatCount(projectStats?.directories)}`}
            />
            <StatTile
              label="Paths"
              value={formatCount(projectStats?.items)}
              detail={`File types: ${formatCount(projectStats?.file_types)} • Latest run: ${latestRunAtText}`}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
            <aside className="rounded-[28px] border border-slate-200 bg-white/90 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950/70">
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Run Queue</p>
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
                    <p className="text-xs text-slate-500 dark:text-slate-400">Filtered from {runs.length.toLocaleString()} loaded runs.</p>
                  </div>
                  <div>
                    <p className="text-sm font-semibold">Auto-refresh while ingesting</p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">Polling every 8 seconds when uploads are active.</p>
                  </div>
                </div>
              </div>

              {topExtensions.length > 0 ? (
                <div className="mt-5">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Top file types</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {topExtensions.map((entry) => (
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
                  return (
                    <article
                      key={run.id}
                      className="rounded-[28px] border border-slate-200 bg-white/90 p-5 shadow-sm transition hover:border-slate-300 dark:border-slate-800 dark:bg-slate-950/70 dark:hover:border-slate-700"
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
                              onClick={() => deleteRun(run.id)}
                            >
                              Delete
                            </button>
                          ) : null}
                        </div>
                      </div>

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
                <div className="rounded-[28px] border border-dashed border-slate-300 bg-white/70 p-8 text-center dark:border-slate-700 dark:bg-slate-950/50">
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
          <div className="rounded-[28px] border border-dashed border-slate-300 bg-white/70 p-8 text-center dark:border-slate-700 dark:bg-slate-950/50">
            <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-500">No Projects</p>
            <h2 className="mt-3 text-2xl font-semibold">Create or select a project to start reviewing scan data.</h2>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
              Once a workspace exists, the collector output, run queue, and inventory views all stay scoped to that project.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
