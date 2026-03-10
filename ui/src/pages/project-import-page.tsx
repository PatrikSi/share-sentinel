import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type Project = { id: string; name: string };

export function ProjectImportPage() {
  const navigate = useNavigate();
  const { projectId } = useParams<{ projectId: string }>();

  const [project, setProject] = useState<Project | null>(null);
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [runName, setRunName] = useState("");
  const [runDescription, setRunDescription] = useState("");
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [importing, setImporting] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}`)
      .then((data) => setProject(data as Project))
      .catch((err) => setError(err.message));
    apiFetch(`/projects/${projectId}/my-role`)
      .then((data) => setProjectRole((data?.role as string) || null))
      .catch(() => setProjectRole(null));
  }, [projectId]);

  async function onImportRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return;
    if (!runName.trim()) {
      setError("Run name is required.");
      return;
    }
    if (!artifactFile) {
      setError("Artifact file is required.");
      return;
    }

    setImporting(true);
    setError(null);
    setInfo(null);

    try {
      const run = (await apiFetch(`/projects/${projectId}/runs`, {
        method: "POST",
        body: JSON.stringify({
          name: runName.trim(),
          description: runDescription.trim() || null,
        }),
      })) as { id: string };

      const formData = new FormData();
      formData.append("file", artifactFile);
      await apiFetch(`/projects/${projectId}/runs/${run.id}/artifact`, {
        method: "POST",
        body: formData,
      });

      setInfo(`Artifact uploaded for run ${run.id}.`);
      navigate(`/projects/${projectId}/runs/${run.id}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setImporting(false);
    }
  }

  const canImport = projectRole === "operator" || projectRole === "admin";
  const artifactName = useMemo(() => artifactFile?.name || "No file selected", [artifactFile]);

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Run Intake</p>
            <h1 className="mt-2 text-3xl font-bold tracking-tight">Import Scan</h1>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{project ? `${project.name} (${project.id})` : projectId}</p>
          </div>
          {projectId ? (
            <Link
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              to="/projects"
            >
              Back to Projects
            </Link>
          ) : null}
        </div>
      </div>

      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-2xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section">
        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <aside className="rounded-[28px] border border-slate-200 bg-[linear-gradient(160deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-5 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(160deg,rgba(15,23,42,0.96),rgba(15,23,42,0.8))]">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Workflow</p>
            <h2 className="mt-2 text-xl font-semibold">Create, attach, ingest</h2>
            <div className="mt-5 space-y-4 text-sm text-slate-600 dark:text-slate-300">
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">1. Describe the run</p>
                <p className="mt-1">Name it clearly so operators can identify scope and timing later.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">2. Upload the artifact</p>
                <p className="mt-1">Compact `.json` and `.json.gz` are preferred. Legacy `.ndjson` remains supported.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">3. Open the run</p>
                <p className="mt-1">The app queues ingest immediately and redirects you to the run detail page.</p>
              </div>
            </div>

            <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Access</p>
              <p className="mt-2 text-sm font-semibold">{projectRole || "Role unavailable"}</p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Only operators and admins can create runs and upload artifacts.</p>
            </div>
          </aside>

          <form className="rounded-[28px] border border-slate-200 bg-white/90 p-6 shadow-sm dark:border-slate-800 dark:bg-slate-950/70" onSubmit={onImportRun}>
            <div className="grid gap-6 md:grid-cols-2">
              <div className="md:col-span-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Step 1</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Run details</h2>
              </div>

              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                Run name
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={runName}
                  onChange={(event) => setRunName(event.target.value)}
                  placeholder="2026-03-10 Corp east sweep"
                  required
                />
              </label>

              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Preferred artifact</p>
                <p className="mt-2 text-sm font-semibold">Compact JSON</p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Use the collector default output or its `.json.gz` variant for smaller uploads.</p>
              </div>

              <label className="md:col-span-2 text-sm font-medium text-slate-700 dark:text-slate-300">
                Description
                <textarea
                  className="mt-2 min-h-[120px] w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={runDescription}
                  onChange={(event) => setRunDescription(event.target.value)}
                  placeholder="Scope, credential set, collection notes, or known coverage gaps"
                />
              </label>

              <div className="md:col-span-2 border-t border-slate-200 pt-6 dark:border-slate-800">
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Step 2</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Artifact upload</h2>
              </div>

              <label className="md:col-span-2 text-sm font-medium text-slate-700 dark:text-slate-300">
                Scan file
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  type="file"
                  accept=".json,.json.gz,.ndjson,.jsonl,.ndjson.gz,.jsonl.gz,.gz,application/json,application/x-ndjson,application/gzip"
                  onChange={(event) => setArtifactFile(event.target.files?.[0] || null)}
                  required
                />
              </label>

              <div className="md:col-span-2 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Selected file</p>
                    <p className="mt-2 text-sm font-semibold">{artifactName}</p>
                  </div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">Accepted: `.json`, `.json.gz`, `.ndjson`, `.jsonl`, and gzip variants.</p>
                </div>
              </div>

              <div className="md:col-span-2 border-t border-slate-200 pt-6 dark:border-slate-800">
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Step 3</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Create run and upload</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">The run record is created first, then the artifact is uploaded and queued for ingest.</p>
              </div>

              <div className="md:col-span-2 flex flex-wrap items-center gap-3">
                <button
                  className="rounded-2xl bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                  type="submit"
                  disabled={!canImport || importing}
                >
                  {importing ? "Creating run and uploading..." : "Create run and upload"}
                </button>
                {!canImport ? <p className="text-sm text-amber-700 dark:text-amber-300">Operator/Admin role required for ingestion.</p> : null}
              </div>
            </div>
          </form>
        </div>
      </div>
    </section>
  );
}
