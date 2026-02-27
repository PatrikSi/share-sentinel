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

  const [runName, setRunName] = useState("");
  const [runDescription, setRunDescription] = useState("");
  const [runIdOverride, setRunIdOverride] = useState("");
  const [targetScopeJson, setTargetScopeJson] = useState('{"cidrs":[],"hosts":[]}');
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [uploadMode, setUploadMode] = useState<"multipart" | "stream">("multipart");
  const [contentTypeOverride, setContentTypeOverride] = useState<"auto" | "application/gzip" | "application/x-ndjson">(
    "auto",
  );
  const [importing, setImporting] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [createdRunId, setCreatedRunId] = useState<string | null>(null);

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
    setCreatedRunId(null);
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    loadRuns(selectedProject, cursor).catch((err) => setError(err.message));
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
      await apiFetch("/projects", {
        method: "POST",
        body: JSON.stringify({ name: newProjectName.trim() }),
      });
      setNewProjectName("");
      setInfo("Project created.");
      await loadProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Project creation failed");
    } finally {
      setCreatingProject(false);
    }
  }

  async function onImportRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject) return;
    if (!runName.trim()) {
      setError("Run name is required.");
      return;
    }

    setImporting(true);
    setError(null);
    setInfo(null);
    setCreatedRunId(null);

    try {
      let parsedScope: Record<string, unknown> = {};
      if (targetScopeJson.trim()) {
        parsedScope = JSON.parse(targetScopeJson) as Record<string, unknown>;
      }

      const runPayload: Record<string, unknown> = {
        name: runName.trim(),
        description: runDescription.trim() || null,
        target_scope: parsedScope,
      };
      if (runIdOverride.trim()) {
        runPayload.run_id = runIdOverride.trim();
      }

      const run = (await apiFetch(`/projects/${selectedProject}/runs`, {
        method: "POST",
        body: JSON.stringify(runPayload),
      })) as Run;
      setCreatedRunId(run.id);
      setInfo(`Run ${run.id} created.`);

      if (artifactFile) {
        if (uploadMode === "multipart") {
          const formData = new FormData();
          const fileForUpload =
            contentTypeOverride === "auto"
              ? artifactFile
              : new File([artifactFile], artifactFile.name, { type: contentTypeOverride });
          formData.append("file", fileForUpload);
          await apiFetch(`/projects/${selectedProject}/runs/${run.id}/artifact`, {
            method: "POST",
            body: formData,
          });
        } else {
          const headers: Record<string, string> = {};
          if (contentTypeOverride !== "auto") {
            headers["Content-Type"] = contentTypeOverride;
          }
          await apiFetch(`/projects/${selectedProject}/runs/${run.id}/artifact`, {
            method: "POST",
            body: artifactFile,
            headers,
          });
        }
        setInfo(`Artifact uploaded for run ${run.id}. Ingestion queued.`);
      } else {
        setInfo(`Run ${run.id} created without artifact.`);
      }

      setRunName("");
      setRunDescription("");
      setRunIdOverride("");
      setArtifactFile(null);
      await loadRuns(selectedProject, cursor);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setImporting(false);
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

  return (
    <section className="space-y-6">
      <div className="panel grid gap-4 md:grid-cols-2">
        <div>
          <h1 className="text-2xl font-bold">Projects & Ingestion</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            BloodHound-style workflow: choose project, create run, upload artifact, and monitor ingestion.
          </p>
        </div>
        <div className="space-y-2">
          <label className="block text-sm font-semibold">Current project</label>
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

      {canCreateProject ? (
        <div className="panel">
          <h2 className="mb-3 text-lg font-semibold">Create Project</h2>
          <form className="flex flex-wrap items-end gap-3" onSubmit={onCreateProject}>
            <label className="flex-1 text-sm">
              Project name
              <input
                className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                placeholder="Client East - Q1"
                required
              />
            </label>
            <button className="rounded-xl bg-pine px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={creatingProject} type="submit">
              {creatingProject ? "Creating..." : "Create"}
            </button>
          </form>
        </div>
      ) : null}

      <div className="panel">
        <h2 className="mb-3 text-lg font-semibold">Import Scan Artifact</h2>
        <form className="grid gap-3 md:grid-cols-2" onSubmit={onImportRun}>
          <label className="text-sm">
            Run name
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={runName}
              onChange={(event) => setRunName(event.target.value)}
              placeholder="Feb 27 corp share sweep"
              required
            />
          </label>

          <label className="text-sm">
            Optional run ID (UUID)
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={runIdOverride}
              onChange={(event) => setRunIdOverride(event.target.value)}
              placeholder="leave empty for auto"
            />
          </label>

          <label className="md:col-span-2 text-sm">
            Description
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={runDescription}
              onChange={(event) => setRunDescription(event.target.value)}
              placeholder="Operator notes, credential set, scope details"
            />
          </label>

          <label className="md:col-span-2 text-sm">
            Target scope JSON
            <textarea
              className="mt-1 h-24 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 font-mono text-xs dark:border-slate-700 dark:bg-slate-900"
              value={targetScopeJson}
              onChange={(event) => setTargetScopeJson(event.target.value)}
            />
          </label>

          <label className="text-sm">
            Upload mode
            <select
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={uploadMode}
              onChange={(event) => setUploadMode(event.target.value as "multipart" | "stream")}
            >
              <option value="multipart">multipart/form-data</option>
              <option value="stream">stream body</option>
            </select>
          </label>

          <label className="text-sm">
            Content type override
            <select
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={contentTypeOverride}
              onChange={(event) =>
                setContentTypeOverride(event.target.value as "auto" | "application/gzip" | "application/x-ndjson")
              }
            >
              <option value="auto">auto-detect</option>
              <option value="application/gzip">application/gzip</option>
              <option value="application/x-ndjson">application/x-ndjson</option>
            </select>
          </label>

          <label className="md:col-span-2 text-sm">
            Artifact file (optional)
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              type="file"
              accept=".ndjson,.gz,.ndjson.gz,application/gzip,application/x-ndjson"
              onChange={(event) => setArtifactFile(event.target.files?.[0] || null)}
            />
          </label>

          <div className="md:col-span-2">
            <button
              className="rounded-xl bg-ember px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              type="submit"
              disabled={!selectedProject || !canImport || importing}
            >
              {importing ? "Importing..." : "Create Run + Upload"}
            </button>
            {!canImport ? <p className="mt-2 text-xs text-amber-700">Operator/Admin role required for ingestion.</p> : null}
          </div>
        </form>
      </div>

      {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
      {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
      {createdRunId ? (
        <p className="text-sm">
          Open created run:{" "}
          <Link className="font-semibold text-ember underline" to={`/projects/${selectedProject}/runs/${createdRunId}`}>
            {createdRunId}
          </Link>
        </p>
      ) : null}

      <div className="panel overflow-x-auto">
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
                      Open
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
