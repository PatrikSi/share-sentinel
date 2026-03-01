import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type UserMe = {
  id: string;
  email: string;
  is_sysadmin: boolean;
};

type Project = { id: string; name: string; created_at: string };

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

export function ProjectsPage() {
  const [me, setMe] = useState<UserMe | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [runs, setRuns] = useState<Run[]>([]);
  const [runSearch, setRunSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [showCreateProjectForm, setShowCreateProjectForm] = useState(false);

  const [projectStats, setProjectStats] = useState<ProjectStats | null>(null);
  const [topExtensions, setTopExtensions] = useState<ExtensionStat[]>([]);
  const [loadingStats, setLoadingStats] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadProjects() {
    const data = (await apiFetch("/projects")) as Project[];
    setProjects(data || []);
    if (!selectedProject && data.length > 0) {
      setSelectedProject(data[0].id);
    }
  }

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
    apiFetch("/auth/me")
      .then((data) => setMe(data as UserMe))
      .catch((err) => setError(err.message));
    loadProjects().catch((err) => setError(err.message));
  }, []);

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
    if (!selectedProject) return;
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

  const selectedProjectName = useMemo(
    () => projects.find((project) => project.id === selectedProject)?.name || "",
    [projects, selectedProject],
  );

  const visibleRuns = useMemo(() => {
    return runs.filter((run) => {
      const statusOk = statusFilter === "all" || run.status === statusFilter;
      const searchOk =
        runSearch.trim() === "" ||
        run.name.toLowerCase().includes(runSearch.toLowerCase()) ||
        (run.description || "").toLowerCase().includes(runSearch.toLowerCase()) ||
        run.id.toLowerCase().includes(runSearch.toLowerCase());
      return statusOk && searchOk;
    });
  }, [runs, runSearch, statusFilter]);
  const latestRun = runs.length > 0 ? runs[0] : null;

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

  async function onCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newProjectName.trim()) return;
    setCreatingProject(true);
    setError(null);
    setInfo(null);

    try {
      const created = (await apiFetch("/projects", {
        method: "POST",
        body: JSON.stringify({ name: newProjectName.trim() }),
      })) as Project;
      setNewProjectName("");
      setSelectedProject(created.id);
      setShowCreateProjectForm(false);
      setInfo(`Project created: ${created.name}`);
      await loadProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Project creation failed");
    } finally {
      setCreatingProject(false);
    }
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Run deletion failed");
    }
  }

  const canCreateProject = !!me?.is_sysadmin;
  const canImport = projectRole === "operator" || projectRole === "admin";
  const canDeleteRuns = projectRole === "admin";
  const latestRunAtText = projectStats?.latest_run_at ? new Date(projectStats.latest_run_at).toLocaleString() : "N/A";

  function formatCount(value: number | null | undefined): string {
    if (value === null || value === undefined) return loadingStats ? "..." : "0";
    return value.toLocaleString();
  }

  return (
    <section className="workspace">
      <div className="workspace-header md:grid-cols-[2fr_1fr]">
        <div>
          <h1 className="text-2xl font-bold">{selectedProjectName || "Projects"}</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Choose a project workspace, browse shares, and manage imported scan runs.
          </p>
          {selectedProject ? (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Link
                className="rounded-xl bg-emerald-600 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-white hover:bg-emerald-500"
                to={`/projects/${selectedProject}/inventory`}
              >
                Browse Shares
              </Link>
              {canImport ? (
                <Link
                  className="rounded-xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  to={`/projects/${selectedProject}/import`}
                >
                  Import Scan
                </Link>
              ) : (
                <span className="rounded-xl border border-slate-200 px-4 py-2 text-xs font-semibold uppercase text-slate-500 dark:border-slate-800 dark:text-slate-400">
                  Import Scan (Operator/Admin)
                </span>
              )}
              {latestRun ? (
                <Link
                  className="rounded-xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  to={`/projects/${selectedProject}/runs/${latestRun.id}`}
                >
                  Open Latest Run
                </Link>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <label className="block text-sm font-semibold">Current project</label>
            {canCreateProject ? (
              <button
                className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                onClick={() => setShowCreateProjectForm((prev) => !prev)}
                type="button"
              >
                {showCreateProjectForm ? "Close" : "New Project"}
              </button>
            ) : null}
          </div>
          <select
            className="w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
            value={selectedProject}
            onChange={(event) => setSelectedProject(event.target.value)}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <p className="text-xs text-slate-500">Your role: {projectRole || "unknown"}</p>
        </div>
      </div>

      {selectedProject ? (
        <div className="workspace-section">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Project Stats</h2>
            <p className="text-xs text-slate-500">Live view across complete and ingesting runs. Latest run: {latestRunAtText}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
              <p className="text-xs uppercase tracking-wide text-slate-500">Runs</p>
              <p className="text-lg font-semibold">{formatCount(projectStats?.runs_total)}</p>
              <p className="text-xs text-slate-500">Scope: {formatCount(projectStats?.scope_runs)}</p>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
              <p className="text-xs uppercase tracking-wide text-slate-500">Endpoints</p>
              <p className="text-lg font-semibold">{formatCount(projectStats?.endpoints)}</p>
              <p className="text-xs text-slate-500">Unique hosts: {formatCount(projectStats?.unique_hosts)}</p>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
              <p className="text-xs uppercase tracking-wide text-slate-500">Shares</p>
              <p className="text-lg font-semibold">{formatCount(projectStats?.shares)}</p>
              <p className="text-xs text-slate-500">Files: {formatCount(projectStats?.files)}</p>
            </div>
            <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
              <p className="text-xs uppercase tracking-wide text-slate-500">Paths</p>
              <p className="text-lg font-semibold">{formatCount(projectStats?.items)}</p>
              <p className="text-xs text-slate-500">
                Directories: {formatCount(projectStats?.directories)} • Types: {formatCount(projectStats?.file_types)}
              </p>
            </div>
          </div>
          {topExtensions.length > 0 ? (
            <div className="mt-3">
              <p className="mb-2 text-xs uppercase tracking-wide text-slate-500">Top file types</p>
              <div className="flex flex-wrap gap-2">
                {topExtensions.map((entry) => (
                  <span
                    key={entry.ext}
                    className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
                  >
                    {entry.ext} ({entry.count.toLocaleString()})
                  </span>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {canCreateProject && showCreateProjectForm ? (
        <div className="workspace-section">
          <h2 className="mb-3 text-lg font-semibold">Create New Project</h2>
          <form className="flex flex-wrap items-end gap-3" onSubmit={onCreateProject}>
            <label className="flex-1 text-sm">
              Project name
              <input
                className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                placeholder="Client East - Q1 Shares"
                required
              />
            </label>
            <button className="rounded-xl bg-pine px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={creatingProject} type="submit">
              {creatingProject ? "Creating..." : "Create"}
            </button>
          </form>
        </div>
      ) : null}

      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section overflow-x-auto">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Runs in {selectedProjectName || "selected project"}</h2>
            <p className="text-xs text-slate-500">Auto-refreshes while runs are ingesting.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search runs"
              value={runSearch}
              onChange={(event) => setRunSearch(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              <option value="all">All statuses</option>
              <option value="PENDING_UPLOAD">PENDING_UPLOAD</option>
              <option value="UPLOADED">UPLOADED</option>
              <option value="INGESTING">INGESTING</option>
              <option value="COMPLETE">COMPLETE</option>
              <option value="FAILED">FAILED</option>
            </select>
            <button
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={movePrev}
              disabled={cursorHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={moveNext}
              disabled={!nextCursor}
            >
              Next
            </button>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Created</th>
              <th>Counts</th>
              <th>Artifact</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {visibleRuns.map((run) => (
              <tr key={run.id}>
                <td>
                  <div className="font-semibold">{run.name}</div>
                  {run.description ? <div className="text-xs text-slate-500">{run.description}</div> : null}
                </td>
                <td>
                  <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${RUN_STATUS_COLORS[run.status] || "bg-slate-200 text-slate-900"}`}>
                    {run.status}
                  </span>
                </td>
                <td>{new Date(run.created_at).toLocaleString()}</td>
                <td>
                  e:{run.summary?.endpoints || 0} r:{run.summary?.resources || 0} i:{run.summary?.items || 0} err:
                  {run.summary?.errors || 0}
                </td>
                <td>{run.artifact_size ? `${Math.round(run.artifact_size / 1024)} KB` : "-"}</td>
                <td>
                  <div className="flex items-center gap-2">
                    <Link
                      className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                      to={`/projects/${selectedProject}/runs/${run.id}`}
                    >
                      Run View
                    </Link>
                    {canDeleteRuns ? (
                      <button
                        className="rounded-lg border border-rose-300 px-3 py-1 text-xs font-semibold uppercase text-rose-700 hover:bg-rose-50 dark:border-rose-700 dark:text-rose-300 dark:hover:bg-rose-950/40"
                        onClick={() => deleteRun(run.id)}
                      >
                        Delete
                      </button>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
