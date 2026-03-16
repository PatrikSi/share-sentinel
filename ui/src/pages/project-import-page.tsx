import { DragEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiUploadFormData } from "@/lib/api";

type Project = { id: string; name: string };

const ACCEPTED_ARTIFACT_SUFFIXES = [".json", ".json.gz", ".ndjson", ".jsonl", ".ndjson.gz", ".jsonl.gz", ".gz"];

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function validateArtifactFile(file: File | null): string | null {
  if (!file) return null;
  if (file.size <= 0) return "The selected artifact is empty.";
  const lowerName = file.name.toLowerCase();
  if (!ACCEPTED_ARTIFACT_SUFFIXES.some((suffix) => lowerName.endsWith(suffix))) {
    return "Supported artifact types are .json, .json.gz, .ndjson, .jsonl, and gzip variants.";
  }
  return null;
}

export function ProjectImportPage() {
  const navigate = useNavigate();
  const { projectId } = useParams<{ projectId: string }>();

  const [project, setProject] = useState<Project | null>(null);
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [runName, setRunName] = useState("");
  const [runDescription, setRunDescription] = useState("");
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [importing, setImporting] = useState(false);
  const [uploadStage, setUploadStage] = useState<"idle" | "creating-run" | "uploading" | "queueing">("idle");
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [uploadTransferredBytes, setUploadTransferredBytes] = useState(0);
  const [uploadTotalBytes, setUploadTotalBytes] = useState(0);

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

  const canImport = projectRole === "operator" || projectRole === "admin";
  const fileValidationError = useMemo(() => validateArtifactFile(artifactFile), [artifactFile]);
  const artifactName = artifactFile?.name || "No file selected";
  const artifactDetectedType = artifactFile ? ACCEPTED_ARTIFACT_SUFFIXES.find((suffix) => artifactFile.name.toLowerCase().endsWith(suffix)) || "custom" : null;
  const uploadProgressPercent =
    uploadStage === "uploading" && uploadTotalBytes > 0 ? Math.min(100, Math.round((uploadTransferredBytes / uploadTotalBytes) * 100)) : 0;

  function stageLabel(): string {
    if (uploadStage === "creating-run") return "Creating the run record and reserving the run ID.";
    if (uploadStage === "uploading") return "Uploading the artifact to the API. Large artifacts can take a while.";
    if (uploadStage === "queueing") return "Artifact stored. Redirecting to the run explorer while the worker picks it up.";
    return "Ready to create a run and upload the artifact.";
  }

  function handleFileSelection(file: File | null) {
    setArtifactFile(file);
    setError(null);
    setInfo(null);
    setUploadTransferredBytes(0);
    setUploadTotalBytes(file?.size || 0);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragActive(false);
    handleFileSelection(event.dataTransfer.files?.[0] || null);
  }

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
    if (fileValidationError) {
      setError(fileValidationError);
      return;
    }

    setImporting(true);
    setUploadStage("creating-run");
    setError(null);
    setInfo(null);
    setCurrentRunId(null);
    setUploadTransferredBytes(0);
    setUploadTotalBytes(artifactFile.size);

    try {
      const run = (await apiFetch(`/projects/${projectId}/runs`, {
        method: "POST",
        body: JSON.stringify({
          name: runName.trim(),
          description: runDescription.trim() || null,
        }),
      })) as { id: string };
      setCurrentRunId(run.id);

      setUploadStage("uploading");
      const formData = new FormData();
      formData.append("file", artifactFile);
      await apiUploadFormData(`/projects/${projectId}/runs/${run.id}/artifact`, formData, {
        onProgress: (loaded, total) => {
          setUploadTransferredBytes(loaded);
          setUploadTotalBytes(total || artifactFile.size);
        },
      });

      setUploadStage("queueing");
      setInfo(`Run ${run.id} created and uploaded. Redirecting to the run explorer for worker status and ingest progress.`);
      navigate(`/projects/${projectId}/runs/${run.id}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
      setUploadStage("idle");
    } finally {
      setImporting(false);
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_360px]">
          <div className="rounded-[28px] border border-slate-200 bg-[linear-gradient(160deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-5 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(160deg,rgba(15,23,42,0.96),rgba(15,23,42,0.8))]">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Run Intake</p>
            <h1 className="mt-2 text-3xl font-bold tracking-tight">Import Scan</h1>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{project ? `${project.name} (${project.id})` : projectId}</p>
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
          </div>

          <div className="rounded-[28px] border border-slate-200 bg-white/90 p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950/70">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Upload Status</p>
            <h2 className="mt-2 text-xl font-semibold">{importing ? "In progress" : "Preflight"}</h2>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{stageLabel()}</p>
            <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
              <div
                className={`h-full rounded-full transition-all ${
                  uploadStage === "uploading"
                    ? "bg-sky-500"
                    : uploadStage === "queueing"
                      ? "bg-emerald-500"
                      : uploadStage === "creating-run"
                        ? "bg-amber-500"
                        : "bg-slate-400"
                }`}
                style={{
                  width:
                    uploadStage === "uploading"
                      ? `${Math.max(uploadProgressPercent, artifactFile ? 6 : 0)}%`
                      : uploadStage === "queueing"
                        ? "100%"
                        : uploadStage === "creating-run"
                          ? "12%"
                          : artifactFile
                            ? "4%"
                            : "0%",
                }}
              />
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Selected file</p>
                <p className="mt-1 text-sm font-semibold">{artifactFile ? "Ready" : "Waiting"}</p>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Access</p>
                <p className="mt-1 text-sm font-semibold">{projectRole || "Role unavailable"}</p>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Transfer</p>
                <p className="mt-1 text-sm font-semibold">
                  {uploadStage === "uploading" ? `${uploadProgressPercent}%` : artifactFile ? "Armed" : "Waiting"}
                </p>
                {uploadStage === "uploading" ? (
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    {formatFileSize(uploadTransferredBytes)} of {formatFileSize(uploadTotalBytes || artifactFile?.size || 0)}
                  </p>
                ) : null}
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Run ID</p>
                <p className="mt-1 text-sm font-semibold">{currentRunId || "Allocated after step 1"}</p>
              </div>
            </div>
            <div className="mt-4 grid gap-2">
              {[
                { key: "creating-run", label: "Create run", detail: currentRunId || "Reserve the run ID in the control plane." },
                {
                  key: "uploading",
                  label: "Transfer artifact",
                  detail:
                    uploadStage === "uploading"
                      ? `${formatFileSize(uploadTransferredBytes)} transferred so far.`
                      : "Stream the collector output into the API.",
                },
                { key: "queueing", label: "Hand off to worker", detail: "Open the run explorer and watch ingest counters update." },
              ].map((stage) => {
                const order = { idle: 0, "creating-run": 1, uploading: 2, queueing: 3 }[uploadStage];
                const stageOrder = { "creating-run": 1, uploading: 2, queueing: 3 }[stage.key as "creating-run" | "uploading" | "queueing"];
                const active = order === stageOrder;
                const done = order > stageOrder;
                return (
                  <div
                    className={`rounded-2xl border px-4 py-3 ${
                      done
                        ? "border-emerald-300 bg-emerald-50 dark:border-emerald-900/40 dark:bg-emerald-900/20"
                        : active
                          ? "border-sky-300 bg-sky-50 dark:border-sky-900/40 dark:bg-sky-900/20"
                          : "border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/80"
                    }`}
                    key={stage.key}
                  >
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">{stage.label}</p>
                    <p className="mt-1 text-sm font-semibold">{done ? "Done" : active ? "Active" : "Waiting"}</p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{stage.detail}</p>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {error ? (
          <StatusBanner tone="error" title="Import Failed">
            <p>{error}</p>
          </StatusBanner>
        ) : null}
        {info ? (
          <StatusBanner tone="success" title="Import Status">
            <p>{info}</p>
          </StatusBanner>
        ) : null}
      </div>

      <div className="workspace-section">
        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <aside className="rounded-[28px] border border-slate-200 bg-[linear-gradient(160deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-5 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(160deg,rgba(15,23,42,0.96),rgba(15,23,42,0.8))]">
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Workflow</p>
            <h2 className="mt-2 text-xl font-semibold">Create, attach, ingest</h2>
            <div className="mt-5 space-y-4 text-sm text-slate-600 dark:text-slate-300">
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">1. Describe the run</p>
                <p className="mt-1">Name it clearly so analysts can spot scope and timing from the dashboard.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">2. Validate the artifact</p>
                <p className="mt-1">Drop a collector file here and confirm the file type, size, and notes before upload.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">3. Monitor ingest</p>
                <p className="mt-1">The app redirects directly to the run explorer so you can watch queue state, ingest counters, and recorded issues.</p>
              </div>
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
                <p className="mt-2 text-sm font-semibold">Compact JSON or gzip variant</p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Use the collector default output when possible for the most direct import path.</p>
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
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Artifact preflight</h2>
              </div>

              <label
                className={`md:col-span-2 flex min-h-[180px] cursor-pointer flex-col items-center justify-center rounded-[28px] border-2 border-dashed px-6 py-8 text-center transition ${
                  dragActive
                    ? "border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20"
                    : "border-slate-300 bg-slate-50/70 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900/40 dark:hover:bg-slate-900/70"
                }`}
                onDragEnter={(event) => {
                  event.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={(event) => {
                  event.preventDefault();
                  if (event.currentTarget === event.target) {
                    setDragActive(false);
                  }
                }}
                onDragOver={(event) => event.preventDefault()}
                onDrop={onDrop}
              >
                <input
                  className="sr-only"
                  type="file"
                  accept=".json,.json.gz,.ndjson,.jsonl,.ndjson.gz,.jsonl.gz,.gz,application/json,application/x-ndjson,application/gzip"
                  onChange={(event) => handleFileSelection(event.target.files?.[0] || null)}
                  required
                />
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Drop zone</p>
                <p className="mt-2 text-lg font-semibold">{dragActive ? "Release to attach the artifact" : "Drag a collector artifact here or click to browse"}</p>
                <p className="mt-2 max-w-xl text-sm text-slate-600 dark:text-slate-300">
                  Accepted: `.json`, `.json.gz`, `.ndjson`, `.jsonl`, and gzip variants.
                </p>
              </label>

              <div className="md:col-span-2 grid gap-3 md:grid-cols-3">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">File name</p>
                  <p className="mt-1 text-sm font-semibold">{artifactName}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Detected type</p>
                  <p className="mt-1 text-sm font-semibold">{artifactDetectedType || "Waiting"}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Size</p>
                  <p className="mt-1 text-sm font-semibold">{artifactFile ? formatFileSize(artifactFile.size) : "Waiting"}</p>
                </div>
              </div>

              {fileValidationError ? (
                <div className="md:col-span-2">
                  <StatusBanner tone="warning" title="Preflight Warning">
                    <p>{fileValidationError}</p>
                  </StatusBanner>
                </div>
              ) : null}

              <div className="md:col-span-2 border-t border-slate-200 pt-6 dark:border-slate-800">
                <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Step 3</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Create run and upload</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                  The run is created first, the artifact upload follows, and the app lands you in the run explorer as ingest begins.
                </p>
              </div>

              <div className="md:col-span-2 flex flex-wrap items-center gap-3">
                <button
                  className="rounded-2xl bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                  type="submit"
                  disabled={!canImport || importing || !!fileValidationError}
                >
                  {importing ? "Uploading artifact..." : "Create run and upload"}
                </button>
                {!canImport ? <p className="text-sm text-amber-700 dark:text-amber-300">Operator or admin access is required for ingestion.</p> : null}
              </div>
            </div>
          </form>
        </div>
      </div>

      {!projectId ? (
        <div className="workspace-section">
          <StatePanel title="No Project Selected" description="Choose a project from the dashboard before importing a run." tone="warning" />
        </div>
      ) : null}
    </section>
  );
}
