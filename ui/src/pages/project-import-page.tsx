import { DragEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiUploadArtifact } from "@/lib/api";

type Project = { id: string; name: string };
type ProjectRoleStatus = "loading" | "ready" | "error";

const ACCEPTED_ARTIFACT_SUFFIXES = [".ndjson.gz", ".jsonl.gz", ".json.gz", ".ndjson", ".jsonl", ".json"] as const;

type ArtifactSuffix = (typeof ACCEPTED_ARTIFACT_SUFFIXES)[number];

function artifactSuffix(file: File | null): ArtifactSuffix | null {
  if (!file) return null;
  const lowerName = file.name.toLowerCase();
  return ACCEPTED_ARTIFACT_SUFFIXES.find((suffix) => lowerName.endsWith(suffix)) || null;
}

function artifactTransport(file: File): {
  filename: string;
  contentType: "application/json" | "application/x-ndjson" | "application/gzip";
} | null {
  const suffix = artifactSuffix(file);
  if (!suffix) return null;
  return {
    filename: `artifact${suffix}`,
    contentType: suffix.endsWith(".gz")
      ? "application/gzip"
      : suffix === ".json"
        ? "application/json"
        : "application/x-ndjson",
  };
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function validateArtifactFile(file: File | null): string | null {
  if (!file) return null;
  if (file.size <= 0) return "The selected artifact is empty.";
  if (!artifactSuffix(file)) {
    return "Supported artifact suffixes are .json, .json.gz, .ndjson, .ndjson.gz, .jsonl, and .jsonl.gz. Bare .gz files are ambiguous and are not accepted.";
  }
  return null;
}

export function ProjectImportPage() {
  const navigate = useNavigate();
  const { projectId } = useParams<{ projectId: string }>();

  const [project, setProject] = useState<Project | null>(null);
  const [projectRole, setProjectRole] = useState<string | null>(null);
  const [projectRoleStatus, setProjectRoleStatus] = useState<ProjectRoleStatus>(projectId ? "loading" : "error");
  const [projectRoleError, setProjectRoleError] = useState<string | null>(null);
  const [projectContextNonce, setProjectContextNonce] = useState(0);

  const [runName, setRunName] = useState("");
  const [runDescription, setRunDescription] = useState("");
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [importing, setImporting] = useState(false);
  const [uploadStage, setUploadStage] = useState<"idle" | "creating-run" | "uploading" | "queueing">("idle");
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [uploadTransferredBytes, setUploadTransferredBytes] = useState(0);
  const [uploadTotalBytes, setUploadTotalBytes] = useState(0);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const importOperationRef = useRef(0);
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;

  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    importOperationRef.current += 1;
    uploadControllerRef.current?.abort();
    uploadControllerRef.current = null;
    setRunName("");
    setRunDescription("");
    setArtifactFile(null);
    setDragActive(false);
    setImporting(false);
    setUploadStage("idle");
    setCurrentRunId(null);
    setUploadTransferredBytes(0);
    setUploadTotalBytes(0);
    return () => {
      importOperationRef.current += 1;
      uploadControllerRef.current?.abort();
    };
  }, [projectId]);

  useEffect(() => {
    if (!importing) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [importing]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setProject(null);
    setProjectRole(null);
    setProjectRoleError(null);
    setError(null);
    apiFetch(`/projects/${projectId}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setProject(data as Project);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !(err instanceof DOMException && err.name === "AbortError")) setError(err.message);
      });
    setProjectRoleStatus("loading");
    apiFetch(`/projects/${projectId}/my-role`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setProjectRole((data?.role as string) || null);
        setProjectRoleStatus("ready");
        setProjectRoleError(null);
      })
      .catch((err) => {
        if (controller.signal.aborted || (err instanceof DOMException && err.name === "AbortError")) return;
        setProjectRole(null);
        setProjectRoleStatus("error");
        setProjectRoleError(err instanceof Error ? err.message : "Project access could not be confirmed.");
      });
    return () => controller.abort();
  }, [projectContextNonce, projectId]);

  const canImport = projectRole === "operator" || projectRole === "admin";
  const fileValidationError = useMemo(() => validateArtifactFile(artifactFile), [artifactFile]);
  const artifactName = artifactFile?.name || "No file selected";
  const artifactDetectedType = artifactSuffix(artifactFile);
  const uploadProgressPercent =
    uploadStage === "uploading" && uploadTotalBytes > 0 ? Math.min(100, Math.round((uploadTransferredBytes / uploadTotalBytes) * 100)) : 0;
  const dropZoneClass = importing
    ? "cursor-not-allowed border-slate-300 bg-slate-100 opacity-70 dark:border-slate-700 dark:bg-slate-900/60"
    : dragActive
      ? "cursor-pointer border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20"
      : "cursor-pointer border-slate-300 bg-slate-50/70 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900/40 dark:hover:bg-slate-900/70";

  function accessLabel(): string {
    if (projectRoleStatus === "loading") return "Checking access";
    if (projectRoleStatus === "error") return "Access check failed";
    return projectRole || "No project access";
  }

  function stageLabel(): string {
    if (uploadStage === "creating-run") return "Creating the run record and reserving the run ID.";
    if (uploadStage === "uploading") return "Uploading the artifact to the API. Large artifacts can take a while.";
    if (uploadStage === "queueing") return "Artifact stored. Redirecting to the run explorer while the worker picks it up.";
    return "Ready to create a run and upload the artifact.";
  }

  function handleFileSelection(file: File | null) {
    if (importing) return;
    setArtifactFile(file);
    setError(null);
    setUploadTransferredBytes(0);
    setUploadTotalBytes(file?.size || 0);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragActive(false);
    if (importing) return;
    handleFileSelection(event.dataTransfer.files?.[0] || null);
  }

  function cancelUpload() {
    uploadControllerRef.current?.abort();
  }

  async function onImportRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return;
    const targetProjectId = projectId;
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
    const transport = artifactTransport(artifactFile);
    if (!transport) {
      setError("The artifact filename does not identify a supported transport format.");
      return;
    }

    const operationId = importOperationRef.current + 1;
    importOperationRef.current = operationId;
    const operationController = new AbortController();
    uploadControllerRef.current?.abort();
    uploadControllerRef.current = operationController;
    setImporting(true);
    setUploadStage("creating-run");
    setError(null);
    const requestedRunId = crypto.randomUUID();
    setCurrentRunId(requestedRunId);
    setUploadTransferredBytes(0);
    setUploadTotalBytes(artifactFile.size);

    let runCreated = false;
    try {
      const run = (await apiFetch(`/projects/${targetProjectId}/runs`, {
        method: "POST",
        signal: operationController.signal,
        body: JSON.stringify({
          run_id: requestedRunId,
          name: runName.trim(),
          description: runDescription.trim() || null,
        }),
      })) as { id: string };
      if (
        importOperationRef.current !== operationId ||
        projectIdRef.current !== targetProjectId ||
        operationController.signal.aborted
      ) return;
      runCreated = true;
      setCurrentRunId(run.id);

      setUploadStage("uploading");
      await apiUploadArtifact(`/projects/${targetProjectId}/runs/${run.id}/artifact`, artifactFile, {
        filename: transport.filename,
        contentType: transport.contentType,
        signal: operationController.signal,
        onProgress: (loaded, total) => {
          if (importOperationRef.current !== operationId || projectIdRef.current !== targetProjectId) return;
          setUploadTransferredBytes(loaded);
          setUploadTotalBytes(total || artifactFile.size);
        },
      });
      if (
        importOperationRef.current !== operationId ||
        projectIdRef.current !== targetProjectId ||
        operationController.signal.aborted
      ) return;

      setUploadStage("queueing");
      navigate(`/projects/${targetProjectId}/runs/${run.id}`, { replace: true });
    } catch (err) {
      if (importOperationRef.current !== operationId || projectIdRef.current !== targetProjectId) return;
      if (err instanceof DOMException && err.name === "AbortError") {
        setError(
          `Upload cancelled. Delivery status for run ${requestedRunId} may be unknown; inspect the run before retrying or deleting it.`,
        );
      } else {
        const detail = err instanceof Error ? err.message : "Import failed.";
        setError(
          runCreated
            ? `${detail} Artifact delivery status for run ${requestedRunId} may be unknown; inspect the run before retrying or deleting it.`
            : `${detail} Creation status for requested run ${requestedRunId} may be unknown; inspect the run before retrying.`,
        );
      }
      setUploadStage("idle");
    } finally {
      if (importOperationRef.current === operationId && projectIdRef.current === targetProjectId) {
        uploadControllerRef.current = null;
        setImporting(false);
      }
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_360px]">
          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Run intake</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">Import scan</h1>
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

          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Upload status</p>
            <h2 className="mt-2 text-xl font-semibold">{importing ? "In progress" : "Preflight"}</h2>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{stageLabel()}</p>
            <div
              aria-label="Import progress"
              aria-valuemax={100}
              aria-valuemin={0}
              aria-valuenow={uploadStage === "queueing" ? 100 : uploadStage === "uploading" ? uploadProgressPercent : uploadStage === "creating-run" ? 12 : 0}
              className="mt-4 h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800"
              role="progressbar"
            >
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
                  <p className="mt-1 text-sm font-semibold">{accessLabel()}</p>
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
                <p className="mt-1 break-all font-mono text-xs font-semibold">{currentRunId || "Reserved when import starts"}</p>
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
          <StatusBanner tone="error" title={currentRunId ? "Import failed" : "Import unavailable"}>
            <p>{error}</p>
            {currentRunId && projectId ? (
              <Link className="mt-2 inline-flex rounded border border-current px-2 py-1 text-xs font-semibold" to={`/projects/${projectId}/runs/${currentRunId}`}>
                Inspect run {currentRunId}
              </Link>
            ) : null}
          </StatusBanner>
        ) : null}
      </div>

      <div className="workspace-section">
        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <aside className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Workflow</p>
            <h2 className="mt-2 text-xl font-semibold">Create, attach, ingest</h2>
            <div className="mt-5 space-y-4 text-sm text-slate-600 dark:text-slate-300">
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">1. Describe the run</p>
                <p className="mt-1">Name it clearly so analysts can spot scope and timing from the dashboard.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">2. Validate the artifact</p>
                <p className="mt-1">Drop an SMB, NFS, or SharePoint collector artifact here and confirm the file type, size, and notes before upload.</p>
              </div>
              <div className="rounded-2xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
                <p className="font-semibold text-slate-900 dark:text-slate-100">3. Monitor ingest</p>
                <p className="mt-1">The app redirects directly to the run explorer so you can watch queue state, ingest counters, and collection issues.</p>
              </div>
            </div>
          </aside>

          <form className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950" onSubmit={onImportRun}>
            <div className="grid gap-6 md:grid-cols-2">
              <div className="md:col-span-2">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Step 1</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Run details</h2>
              </div>

              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                Run name
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  disabled={importing}
                  value={runName}
                  onChange={(event) => setRunName(event.target.value)}
                  placeholder="2026-03-10 Corp east sweep"
                  required
                />
              </label>

              <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Preferred artifact</p>
                <p className="mt-2 text-sm font-semibold">Streaming NDJSON or compressed NDJSON</p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Use the collector's `.ndjson` / `.ndjson.gz` default for large scans. Compact `.json` is a compatibility format limited to 50 MiB.</p>
              </div>

              <div className="md:col-span-2">
                <StatusBanner tone="info" title="Collection credentials stay on the collector host">
                  <p>
                    SharePoint authentication runs in the local collector, not in this browser. Uploaded artifacts retain non-secret provider, identity, scope, and completeness context so analysts can distinguish application inventory from a delegated user's view.
                  </p>
                  <p className="mt-1">SharePoint collection inventories filenames, folders, paths, identifiers, and metadata. It does not download document content.</p>
                </StatusBanner>
              </div>

              <label className="md:col-span-2 text-sm font-medium text-slate-700 dark:text-slate-300">
                Description
                <textarea
                  className="mt-2 min-h-[120px] w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  disabled={importing}
                  value={runDescription}
                  onChange={(event) => setRunDescription(event.target.value)}
                  placeholder="Scope, collector identity label, collection notes, or known coverage gaps (never paste secrets)"
                />
              </label>

              <div className="md:col-span-2 border-t border-slate-200 pt-6 dark:border-slate-800">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Step 2</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Artifact preflight</h2>
              </div>

              <label
                aria-disabled={importing}
                className={`md:col-span-2 flex min-h-[150px] flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-6 text-center transition ${dropZoneClass}`}
                onDragEnter={(event) => {
                  event.preventDefault();
                  if (!importing) setDragActive(true);
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
                  disabled={importing}
                  type="file"
                  accept=".json,.json.gz,.ndjson,.jsonl,.ndjson.gz,.jsonl.gz"
                  onChange={(event) => handleFileSelection(event.target.files?.[0] || null)}
                  required
                />
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Drop zone</p>
                <p className="mt-2 text-lg font-semibold">{dragActive ? "Release to attach the artifact" : "Drag a collector artifact here or click to browse"}</p>
                <p className="mt-2 max-w-xl text-sm text-slate-600 dark:text-slate-300">
                  Accepted: `.json`, `.json.gz`, `.ndjson`, `.ndjson.gz`, `.jsonl`, and `.jsonl.gz`.
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
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Step 3</p>
                <h2 className="mt-2 text-2xl font-semibold tracking-tight">Create run and upload</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                  The run is created first, the artifact upload follows, and the app lands you in the run explorer as ingest begins.
                </p>
              </div>

              <div className="md:col-span-2 flex flex-wrap items-center gap-3">
                <button
                  className="rounded-2xl bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                  type="submit"
                  disabled={projectRoleStatus !== "ready" || !canImport || importing || !!fileValidationError}
                >
                  {importing ? "Uploading artifact..." : "Create run and upload"}
                </button>
                {uploadStage === "uploading" ? (
                  <button
                    className="rounded-lg border border-rose-300 px-4 py-2.5 text-sm font-semibold text-rose-700 transition hover:bg-rose-50 dark:border-rose-800 dark:text-rose-200 dark:hover:bg-rose-900/20"
                    onClick={cancelUpload}
                    type="button"
                  >
                    Cancel upload
                  </button>
                ) : null}
                {projectRoleStatus === "loading" ? (
                  <p className="text-sm text-slate-500 dark:text-slate-400">Checking project access before enabling upload.</p>
                ) : null}
                {projectRoleStatus === "error" ? (
                  <div className="flex flex-wrap items-center gap-2 text-sm text-amber-700 dark:text-amber-300" role="alert">
                    <span>
                      Project access could not be confirmed. {projectRoleError || "The role service did not return a usable response."} Upload remains disabled.
                    </span>
                    <button
                      className="rounded-md border border-current px-3 py-2 text-xs font-semibold"
                      onClick={() => setProjectContextNonce((current) => current + 1)}
                      type="button"
                    >
                      Retry access check
                    </button>
                  </div>
                ) : null}
                {projectRoleStatus === "ready" && !canImport ? (
                  <p className="text-sm text-amber-700 dark:text-amber-300">Operator or admin access is required for ingestion.</p>
                ) : null}
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
