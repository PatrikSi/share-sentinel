import { FormEvent, useEffect, useState } from "react";
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

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">Import Scan</h1>
            <p className="text-sm text-slate-600 dark:text-slate-300">{project ? `${project.name} (${project.id})` : projectId}</p>
          </div>
          {projectId ? (
            <Link
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              to="/projects"
            >
              Back to Projects
            </Link>
          ) : null}
        </div>
      </div>

      <div className="workspace-section">
        <h2 className="mb-3 text-lg font-semibold">Upload Artifact</h2>
        <form className="grid gap-3 md:grid-cols-2" onSubmit={onImportRun}>
          <label className="text-sm">
            Run name
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={runName}
              onChange={(event) => setRunName(event.target.value)}
              placeholder="Mar 01 subnet scan"
              required
            />
          </label>

          <label className="md:col-span-2 text-sm">
            Description
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              value={runDescription}
              onChange={(event) => setRunDescription(event.target.value)}
              placeholder="Operator notes, scope, credentials set"
            />
          </label>

          <label className="md:col-span-2 text-sm">
            Scan file
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              type="file"
              accept=".json,.jsonl,.ndjson,.json.gz,.ndjson.gz,.gz,application/json,application/x-ndjson,application/gzip"
              onChange={(event) => setArtifactFile(event.target.files?.[0] || null)}
              required
            />
            <p className="mt-1 text-xs text-slate-500">Accepted: .json, .jsonl, .ndjson, and .gz variants.</p>
          </label>

          <div className="md:col-span-2">
            <button
              className="rounded-xl bg-ember px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              type="submit"
              disabled={!canImport || importing}
            >
              {importing ? "Importing..." : "Create Run + Upload"}
            </button>
            {!canImport ? <p className="mt-2 text-xs text-amber-700">Operator/Admin role required for ingestion.</p> : null}
          </div>
        </form>
      </div>

      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
