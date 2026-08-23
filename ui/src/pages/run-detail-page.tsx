import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { useSession } from "@/lib/auth";
import { copyText } from "@/lib/clipboard";

type RunProgress = {
  line_offset?: number;
  last_error?: string;
  attempt_count?: number;
  next_retry_at?: string;
  [key: string]: unknown;
};

type RunInfo = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  artifact_size: number | null;
  artifact_sha256: string | null;
  artifact_content_type: string | null;
  target_scope: Record<string, unknown>;
  ingest_progress: RunProgress;
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
type Item = { id: number; path: string; is_dir: boolean; resource_id?: number; name?: string; size_bytes?: number | null; mtime?: string | null };
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
type RunIssueSeverity = "all" | "error" | "warn";
type RunIssue = {
  id: number;
  severity: Exclude<RunIssueSeverity, "all">;
  code: string;
  message: string;
  endpoint_key: string | null;
  resource_name: string | null;
  path: string | null;
  created_at: string;
};
type RunActivityEvent = {
  id: number;
  ts: string;
  action: string;
  object_type: string;
  object_id: string;
  metadata: Record<string, unknown>;
};
type RunDetailTab = "overview" | "issues" | "diff" | "explore" | "search";
const RUN_DETAIL_TABS: RunDetailTab[] = ["overview", "issues", "diff", "explore", "search"];

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
  issues: {
    label: "Issues",
    description: "Inspect ingest warnings and errors with host, share, and path context.",
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

function parseLineOffset(progress: RunProgress | null | undefined): number {
  const raw = progress?.line_offset;
  const parsed = typeof raw === "number" ? raw : Number.parseInt(String(raw ?? "0"), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function issueSeverityTone(severity: Exclude<RunIssueSeverity, "all">): string {
  if (severity === "warn") {
    return "border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200";
  }
  return "border-rose-300 bg-rose-100 text-rose-700 dark:border-rose-900/40 dark:bg-rose-900/20 dark:text-rose-200";
}

function formatBytes(value: number | null | undefined): string {
  if (!value || value <= 0) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function activityTitle(action: string): string {
  switch (action) {
    case "RUN_CREATED":
      return "Run created";
    case "ARTIFACT_UPLOADED":
      return "Artifact uploaded";
    case "INGEST_QUEUED":
      return "Ingest queued";
    case "INGEST_QUEUE_FALLBACK":
      return "Ingest queued via fallback";
    case "INGEST_STARTED":
      return "Worker started ingest";
    case "INGEST_RETRY_SCHEDULED":
      return "Ingest retry scheduled";
    case "INGEST_COMPLETED":
      return "Worker completed ingest";
    case "INGEST_FAILED":
      return "Worker failed ingest";
    default:
      return action.replaceAll("_", " ").toLowerCase();
  }
}

function activityDetail(event: RunActivityEvent): string {
  if (event.action === "ARTIFACT_UPLOADED") {
    const size = typeof event.metadata.size === "number" ? event.metadata.size : Number(event.metadata.size || 0);
    const contentType = typeof event.metadata.content_type === "string" ? event.metadata.content_type : "unknown type";
    return `Stored ${formatBytes(size)} as ${contentType}.`;
  }

  if (event.action === "INGEST_STARTED") {
    const resumeFromLine = Number(event.metadata.resume_from_line || 0);
    const worker = typeof event.metadata.worker === "string" ? event.metadata.worker : "worker";
    return `${worker} started from line ${resumeFromLine.toLocaleString()}.`;
  }

  if (event.action === "INGEST_COMPLETED") {
    const counts = (event.metadata.counts || {}) as Record<string, unknown>;
    const lineOffset = Number(event.metadata.line_offset || 0);
    return `Finished at line ${lineOffset.toLocaleString()} with ${Number(counts.endpoints || 0).toLocaleString()} endpoints, ${Number(counts.resources || 0).toLocaleString()} shares, ${Number(counts.items || 0).toLocaleString()} items, and ${Number(counts.errors || 0).toLocaleString()} issues.`;
  }

  if (event.action === "INGEST_FAILED") {
    return typeof event.metadata.error === "string" ? event.metadata.error : "The worker reported a failure.";
  }

  if (event.action === "INGEST_RETRY_SCHEDULED") {
    const attempt = Number(event.metadata.attempt_count || 0);
    const nextRetryAt = typeof event.metadata.next_retry_at === "string" ? event.metadata.next_retry_at : null;
    const error = typeof event.metadata.error === "string" ? event.metadata.error : "The previous ingest attempt failed.";
    const parsedRetry = nextRetryAt ? new Date(nextRetryAt) : null;
    const retryLabel = parsedRetry && !Number.isNaN(parsedRetry.getTime()) ? parsedRetry.toLocaleString() : "the scheduled retry time";
    return `${error} Retry attempt ${Math.max(1, attempt + 1)} is scheduled for ${retryLabel}.`;
  }

  if (event.action === "INGEST_QUEUED" || event.action === "INGEST_QUEUE_FALLBACK") {
    return "The artifact has been handed off to the background worker queue.";
  }

  if (event.action === "RUN_CREATED") {
    return "The run record was created and is ready for artifact upload.";
  }

  return "Recorded run activity.";
}

function describeRunStatus(run: RunInfo | null) {
  if (!run) {
    return {
      headline: "Loading run status",
      detail: "Fetching current queue state, ingest checkpoint, and summary counters.",
      progressTone: "bg-slate-400",
      progressWidth: "8%",
      animate: true,
      metaLabel: "Checkpoint",
      metaValue: "Waiting for run metadata",
      lastError: null as string | null,
    };
  }

  const lineOffset = parseLineOffset(run.ingest_progress);
  const issueCount = run.summary?.errors || 0;
  const lastError = typeof run.ingest_progress?.last_error === "string" ? run.ingest_progress.last_error : null;
  const attemptCount = Math.max(0, Number(run.ingest_progress?.attempt_count || 0));
  const nextRetryAt = typeof run.ingest_progress?.next_retry_at === "string" ? run.ingest_progress.next_retry_at : null;
  const parsedRetryAt = nextRetryAt ? new Date(nextRetryAt) : null;
  const retryAtLabel = parsedRetryAt && !Number.isNaN(parsedRetryAt.getTime()) ? parsedRetryAt.toLocaleString() : null;

  if (run.status === "PENDING_UPLOAD") {
    return {
      headline: "Waiting for artifact upload",
      detail: "The run record exists, but no artifact has been attached yet.",
      progressTone: "bg-slate-400",
      progressWidth: "10%",
      animate: false,
      metaLabel: "Artifact",
      metaValue: "Upload not started",
      lastError,
    };
  }

  if (run.status === "UPLOADED") {
    if (attemptCount > 0 || nextRetryAt || lastError) {
      return {
        headline: "Ingest retry scheduled",
        detail: `${lastError || "The previous ingest attempt did not complete."} ${retryAtLabel ? `The next attempt is scheduled for ${retryAtLabel}.` : "The worker will retry when the recovery schedule permits."}`,
        progressTone: "bg-amber-500",
        progressWidth: "44%",
        animate: true,
        metaLabel: "Retry",
        metaValue: `Attempt ${attemptCount + 1}${retryAtLabel ? ` · ${retryAtLabel}` : " pending"}`,
        lastError,
      };
    }
    return {
      headline: "Artifact queued for worker pickup",
      detail: "Upload finished. The worker has not started parsing the artifact yet.",
      progressTone: "bg-amber-500",
      progressWidth: "32%",
      animate: true,
      metaLabel: "Checkpoint",
      metaValue: lineOffset > 0 ? `Line ${lineOffset.toLocaleString()}` : "Waiting for first checkpoint",
      lastError,
    };
  }

  if (run.status === "INGESTING") {
    return {
      headline: "Worker is ingesting collector output",
      detail:
        lineOffset > 0
          ? `The worker has confirmed progress through line ${lineOffset.toLocaleString()}. Counts below update as records are committed.`
          : "The worker has started, but no progress checkpoint has been written yet.",
      progressTone: "bg-sky-500",
      progressWidth: "72%",
      animate: true,
      metaLabel: "Checkpoint",
      metaValue: lineOffset > 0 ? `Line ${lineOffset.toLocaleString()}` : "Starting parse",
      lastError,
    };
  }

  if (run.status === "FAILED") {
    return {
      headline: "Ingest failed before completion",
      detail: lastError || "The worker reported a failure. Review the recorded issues and the last checkpoint below.",
      progressTone: "bg-rose-500",
      progressWidth: "100%",
      animate: false,
      metaLabel: "Last checkpoint",
      metaValue: lineOffset > 0 ? `Line ${lineOffset.toLocaleString()}` : "No checkpoint recorded",
      lastError,
    };
  }

  if (issueCount > 0) {
    return {
      headline: "Ingest completed with recorded issues",
      detail: `The ingest finished, but ${issueCount.toLocaleString()} warning or error record${issueCount === 1 ? "" : "s"} ${issueCount === 1 ? "was" : "were"} stored for operator review.`,
      progressTone: "bg-amber-500",
      progressWidth: "100%",
      animate: false,
      metaLabel: "Checkpoint",
      metaValue: lineOffset > 0 ? `Line ${lineOffset.toLocaleString()}` : "Completed",
      lastError,
    };
  }

  return {
    headline: "Ingest completed cleanly",
    detail: "The worker finished parsing the artifact and no ingest issues were recorded.",
    progressTone: "bg-emerald-500",
    progressWidth: "100%",
    animate: false,
    metaLabel: "Checkpoint",
    metaValue: lineOffset > 0 ? `Line ${lineOffset.toLocaleString()}` : "Completed",
    lastError,
  };
}

export function RunDetailPage() {
  const { projectId, runId } = useParams<{ projectId: string; runId: string }>();
  const session = useSession();

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
  const [issues, setIssues] = useState<RunIssue[]>([]);
  const [issuesLoading, setIssuesLoading] = useState(false);
  const [issuesError, setIssuesError] = useState<string | null>(null);
  const [issueSearch, setIssueSearch] = useState("");
  const [issueSeverity, setIssueSeverity] = useState<RunIssueSeverity>("all");
  const [issueCursor, setIssueCursor] = useState<string | null>(null);
  const [issueHistory, setIssueHistory] = useState<Array<string | null>>([]);
  const [issueNext, setIssueNext] = useState<string | null>(null);
  const [selectedIssueId, setSelectedIssueId] = useState<number | null>(null);
  const [activityEvents, setActivityEvents] = useState<RunActivityEvent[]>([]);
  const [activityLoading, setActivityLoading] = useState(false);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [artifactCopyStatus, setArtifactCopyStatus] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const savedQueriesKey = useMemo(() => {
    const userId = session.user?.id || "anonymous";
    const scopedProjectId = projectId || "no-project";
    return `share_sentinel_saved_queries_${userId}_${scopedProjectId}_${runId || "default"}`;
  }, [projectId, runId, session.user?.id]);
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
    const controller = new AbortController();
    setRun(null);
    setError(null);
    apiFetch(`/projects/${projectId}/runs/${runId}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setRun(data as RunInfo);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setProjectRuns([]);
    apiFetchAllPages<RunCompareOption>((cursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (cursor) query.set("cursor", cursor);
      return `/projects/${projectId}/runs?${query.toString()}`;
    }, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setProjectRuns(data);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce]);

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
    setIssueSearch("");
    setIssueSeverity("all");
    setIssueCursor(null);
    setIssueHistory([]);
    setIssueNext(null);
    setIssues([]);
    setIssuesError(null);
    setSelectedIssueId(null);
    setActivityEvents([]);
    setActivityError(null);
    setArtifactCopyStatus(null);
    setEndpointSearch("");
    setItemSearch("");
    setPathPrefix("");
    setGlobalQuery("");
    setGlobalExt("");
    setEndpoints([]);
    setResources([]);
    setItems([]);
    setGlobalItems([]);
    setSelectedEndpoint(null);
    setSelectedResource(null);
    setEndpointCursor(null);
    setEndpointHistory([]);
    setEndpointNext(null);
    setItemCursor(null);
    setItemHistory([]);
    setItemNext(null);
    setGlobalCursor(null);
    setGlobalHistory([]);
    setGlobalNext(null);
    setActiveTab("overview");
  }, [projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const controller = new AbortController();
    setDiffLoading(true);
    setDiffError(null);
    setRunDiff(null);
    const query = new URLSearchParams();
    if (selectedBaselineRunId) query.set("baseline_run_id", selectedBaselineRunId);
    const suffix = query.toString() ? `?${query.toString()}` : "";

    apiFetch(`/projects/${projectId}/runs/${runId}/diff${suffix}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const payload = data as RunDiffResult;
        setRunDiff(payload);
        if (!selectedBaselineRunId && payload.baseline_run?.id) {
          setSelectedBaselineRunId(payload.baseline_run.id);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setDiffError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDiffLoading(false);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, selectedBaselineRunId]);

  const shouldPollRun = run?.status === "UPLOADED" || run?.status === "INGESTING";

  useEffect(() => {
    if (!projectId || !runId || !shouldPollRun) return;
    let stopped = false;
    let timer: number | null = null;
    let refreshController: AbortController | null = null;

    const poll = async () => {
      const tickController = new AbortController();
      refreshController = tickController;
      try {
        const data = await apiFetch(`/projects/${projectId}/runs/${runId}`, { signal: tickController.signal });
        if (!stopped && !tickController.signal.aborted) setRun(data as RunInfo);
      } catch (err) {
        if (!stopped && !tickController.signal.aborted && !isAbortError(err)) {
          setError(`Live run refresh is delayed; showing the last confirmed state. ${err instanceof Error ? err.message : "Retry when the API is available."}`);
        }
      }
      if (stopped || tickController.signal.aborted) return;
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
  }, [projectId, reloadNonce, runId, shouldPollRun]);

  useEffect(() => {
    setIssueCursor(null);
    setIssueHistory([]);
  }, [issueSearch, issueSeverity, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    if ((run?.summary?.errors || 0) === 0 && activeTab !== "issues") {
      setIssues([]);
      setIssueNext(null);
      setIssuesError(null);
      setSelectedIssueId(null);
      return;
    }

    const query = new URLSearchParams({ limit: "50" });
    if (issueSearch.trim()) query.set("search", issueSearch.trim());
    if (issueSeverity !== "all") query.set("severity", issueSeverity);
    if (issueCursor) query.set("cursor", issueCursor);

    const controller = new AbortController();
    setIssuesLoading(true);
    setIssuesError(null);
    setIssues([]);
    setSelectedIssueId(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/errors?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const rows = (data?.items || []) as RunIssue[];
        setIssues(rows);
        setIssueNext((data?.next_cursor as string | null) || null);
        setSelectedIssueId((current) => {
          if (current && rows.some((issue) => issue.id === current)) {
            return current;
          }
          return rows[0]?.id || null;
        });
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setIssuesError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setIssuesLoading(false);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, issueSearch, issueSeverity, issueCursor, activeTab, run?.summary?.errors, run?.status]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const controller = new AbortController();
    setActivityLoading(true);
    setActivityError(null);
    setActivityEvents([]);
    apiFetch(`/projects/${projectId}/runs/${runId}/activity?limit=12`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setActivityEvents((data?.items || []) as RunActivityEvent[]);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setActivityError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setActivityLoading(false);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, run?.status, run?.summary?.errors]);

  useEffect(() => {
    setEndpointCursor(null);
    setEndpointHistory([]);
  }, [endpointSearch, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "100", search: endpointSearch });
    if (endpointCursor) query.set("cursor", endpointCursor);

    const controller = new AbortController();
    setEndpoints([]);
    setSelectedEndpoint(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
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
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, endpointSearch, endpointCursor]);

  useEffect(() => {
    if (!projectId || !runId || !selectedEndpoint) {
      setResources([]);
      setSelectedResource(null);
      return;
    }

    const controller = new AbortController();
    setResources([]);
    setSelectedResource(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const rows = (data?.items || []) as Resource[];
        setResources(rows);
        setSelectedResource((current) => {
          if (current && rows.some((resource) => resource.id === current)) {
            return current;
          }
          return rows[0]?.id || null;
        });
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, selectedEndpoint]);

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

    const controller = new AbortController();
    setItems([]);
    setItemNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/resources/${selectedResource}/items?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setItems((data?.items || []) as Item[]);
        setItemNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, selectedResource, itemSearch, pathPrefix, itemCursor]);

  useEffect(() => {
    setGlobalCursor(null);
    setGlobalHistory([]);
  }, [globalQuery, globalExt, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "200", q: globalQuery });
    if (globalExt) query.set("ext", globalExt);
    if (globalCursor) query.set("cursor", globalCursor);

    const controller = new AbortController();
    setGlobalItems([]);
    setGlobalNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/search/items?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setGlobalItems((data?.items || []) as Item[]);
        setGlobalNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, reloadNonce, runId, globalQuery, globalExt, globalCursor]);

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

  async function copyArtifactSha256() {
    if (!run?.artifact_sha256) return;
    try {
      await copyText(run.artifact_sha256);
      setArtifactCopyStatus("SHA-256 copied");
    } catch {
      setArtifactCopyStatus("Copy failed; select the hash manually.");
    }
  }

  function openIssueInSearch(issue: RunIssue) {
    setGlobalQuery(issue.path || issue.resource_name || issue.endpoint_key || issue.code);
    setGlobalExt("");
    setActiveTab("search");
  }

  function focusIssueEndpoint(issue: RunIssue) {
    setEndpointSearch(issue.endpoint_key || "");
    setActiveTab("explore");
  }

  function retryRunData() {
    setError(null);
    setDiffError(null);
    setIssuesError(null);
    setActivityError(null);
    setReloadNonce((current) => current + 1);
  }

  function handleTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, tab: RunDetailTab) {
    const currentIndex = RUN_DETAIL_TABS.indexOf(tab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % RUN_DETAIL_TABS.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + RUN_DETAIL_TABS.length) % RUN_DETAIL_TABS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = RUN_DETAIL_TABS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = RUN_DETAIL_TABS[nextIndex];
    setActiveTab(nextTab);
    document.getElementById(`run-tab-${nextTab}`)?.focus();
  }

  function formatScopeValue(value: unknown): string {
    if (Array.isArray(value)) return value.join(", ");
    if (value && typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  const runStatus = useMemo(() => describeRunStatus(run), [run]);
  const selectedIssue = useMemo(() => issues.find((issue) => issue.id === selectedIssueId) || null, [issues, selectedIssueId]);
  const issuePreview = useMemo(() => issues.slice(0, 3), [issues]);
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
          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Run explorer</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">{run?.name || "Run explorer"}</h1>
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

          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Run status</p>
            {run ? (
              <>
                <div className="mt-2 flex items-center gap-3">
                  <span className={`rounded-full px-3 py-1 text-xs font-semibold ${RUN_STATUS_COLORS[run.status] || "bg-slate-200 text-slate-900"}`}>
                    {run.status}
                  </span>
                  <span className="text-sm text-slate-500">Created {new Date(run.created_at).toLocaleString()}</span>
                </div>
                <p className="mt-4 text-base font-semibold">{runStatus.headline}</p>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{runStatus.detail}</p>
                <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                  <div
                    className={`h-full rounded-full ${runStatus.progressTone} ${runStatus.animate ? "animate-pulse" : ""}`}
                    style={{ width: runStatus.progressWidth }}
                  />
                </div>
                <div className="mt-4 space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">{runStatus.metaLabel}</p>
                      <p className="mt-1 text-sm font-semibold">{runStatus.metaValue}</p>
                    </div>
                    <div>
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Issue count</p>
                      <p className="mt-1 text-sm font-semibold">{(run.summary?.errors || 0).toLocaleString()}</p>
                    </div>
                  </div>
                  {runStatus.lastError ? (
                    <div>
                      <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Last worker error</p>
                      <p className="mt-1 text-sm text-rose-700 dark:text-rose-300">{runStatus.lastError}</p>
                    </div>
                  ) : null}
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

        <div aria-label="Run explorer sections" className="run-detail-tabs" role="tablist">
          {RUN_DETAIL_TABS.map((tab) => (
            <button
              aria-controls={`run-panel-${tab}`}
              aria-selected={activeTab === tab}
              className={`rounded-md border px-3 py-2 text-sm font-semibold transition ${
                activeTab === tab
                  ? "border-emerald-600 bg-emerald-50 text-emerald-900 dark:bg-emerald-900/20 dark:text-emerald-100"
                  : "border-transparent text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              }`}
              id={`run-tab-${tab}`}
              key={tab}
              onClick={() => setActiveTab(tab)}
              onKeyDown={(event) => handleTabKeyDown(event, tab)}
              role="tab"
              tabIndex={activeTab === tab ? 0 : -1}
              type="button"
            >
              {RUN_DETAIL_TAB_COPY[tab].label}
              {tab === "issues" && (run?.summary?.errors || 0) > 0 ? <span className="ml-2 rounded-full bg-rose-100 px-1.5 py-0.5 text-[10px] text-rose-700 dark:bg-rose-900/40 dark:text-rose-200">{run?.summary?.errors}</span> : null}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-500 dark:text-slate-400">{RUN_DETAIL_TAB_COPY[activeTab].description}</p>

        {error ? (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200" role="alert">
            <span>{error}</span>
            <button className="rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">Retry run data</button>
          </div>
        ) : null}
      </div>

      {activeTab === "overview" ? (
        <div aria-labelledby="run-tab-overview" className="workspace-section space-y-4" id="run-panel-overview" role="tabpanel" tabIndex={0}>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_360px]">
            <div className="workspace-card space-y-4">
              <div>
                <h2 className="text-lg font-semibold">Run Summary</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  Use the tabs below to move between issue review, diff analysis, hierarchical exploration, and targeted search without losing operational context.
                </p>
              </div>
              {(run?.summary?.errors || 0) > 0 ? (
                <StatusBanner tone="warning" title="Recorded Ingest Issues">
                  <div className="space-y-3">
                    <p>
                      {(run?.summary?.errors || 0).toLocaleString()} warning or error record{(run?.summary?.errors || 0) === 1 ? "" : "s"} {((run?.summary?.errors || 0) === 1) ? "was" : "were"} captured during ingest.
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <button
                        className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                        onClick={() => setActiveTab("issues")}
                        type="button"
                      >
                        Review Issues
                      </button>
                      {issuePreview.map((issue) => (
                        <button
                          className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] ${issueSeverityTone(issue.severity)}`}
                          key={issue.id}
                          onClick={() => {
                            setSelectedIssueId(issue.id);
                            setActiveTab("issues");
                          }}
                          type="button"
                        >
                          {issue.code}
                        </button>
                      ))}
                    </div>
                    {issuePreview.length > 0 ? (
                      <div className="grid gap-3">
                        {issuePreview.map((issue) => (
                          <button
                            className="rounded-2xl border border-amber-300/80 bg-white/60 px-4 py-3 text-left transition hover:bg-white/90 dark:border-amber-900/40 dark:bg-slate-950/30 dark:hover:bg-slate-950/50"
                            key={`preview:${issue.id}`}
                            onClick={() => {
                              setSelectedIssueId(issue.id);
                              setActiveTab("issues");
                            }}
                            type="button"
                          >
                            <div className="flex flex-wrap items-center gap-2">
                              <span className={`rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${issueSeverityTone(issue.severity)}`}>
                                {issue.severity}
                              </span>
                              <span className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-600 dark:text-slate-300">{issue.code}</span>
                            </div>
                            <p className="mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100">{issue.message}</p>
                            <div className="mt-2 space-y-1 text-xs text-slate-600 dark:text-slate-300">
                              {issue.endpoint_key ? <p>Host: {issue.endpoint_key}</p> : null}
                              {issue.resource_name ? <p>Share: {issue.resource_name}</p> : null}
                              {issue.path ? <p className="font-mono break-all">{issue.path}</p> : null}
                            </div>
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </StatusBanner>
              ) : run?.status === "INGESTING" ? (
                <StatusBanner tone="info" title="Live Ingest Status">
                  <p>The worker is actively parsing this artifact. Re-open the Issues tab if you want to watch new warnings and errors arrive.</p>
                </StatusBanner>
              ) : null}
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
                  onClick={() => setActiveTab("issues")}
                  type="button"
                >
                  Open Issues
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
                <h2 className="text-lg font-semibold">Operational Snapshot</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  Queue state, worker checkpoint, and run-to-run context in one place for triage.
                </p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Artifact size</p>
                  <p className="mt-1 text-sm font-semibold">{run?.artifact_size ? formatBytes(run.artifact_size) : "Not uploaded"}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Worker checkpoint</p>
                  <p className="mt-1 text-sm font-semibold">
                    {parseLineOffset(run?.ingest_progress) > 0 ? `Line ${parseLineOffset(run?.ingest_progress).toLocaleString()}` : "No checkpoint yet"}
                  </p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Content type</p>
                  <p className="mt-1 truncate text-sm font-semibold" title={run?.artifact_content_type || undefined}>{run?.artifact_content_type || "Not recorded"}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Artifact SHA-256</p>
                    {run?.artifact_sha256 ? <button className="rounded border border-slate-300 px-2 py-1 text-[10px] font-semibold hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800" onClick={copyArtifactSha256} type="button">Copy</button> : null}
                  </div>
                  <code className="mt-1 block truncate font-mono text-xs" title={run?.artifact_sha256 || undefined}>{run?.artifact_sha256 || "Not recorded"}</code>
                  {artifactCopyStatus ? <p aria-live="polite" className="mt-1 text-xs text-slate-500">{artifactCopyStatus}</p> : null}
                </div>
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
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Run Activity</p>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Collector lifecycle and worker checkpoints for this run.</p>
                  </div>
                  {activityLoading ? <span className="text-xs text-slate-500">Refreshing...</span> : null}
                </div>
                {activityError ? <p className="mt-3 text-sm text-rose-700 dark:text-rose-300">{activityError}</p> : null}
                {activityEvents.length > 0 ? (
                  <ol className="mt-4 space-y-3">
                    {activityEvents.map((event) => (
                      <li className="border-l-2 border-slate-300 pl-4 dark:border-slate-700" key={event.id}>
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="text-sm font-semibold">{activityTitle(event.action)}</p>
                          <span className="text-xs text-slate-500">{new Date(event.ts).toLocaleString()}</span>
                        </div>
                        <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{activityDetail(event)}</p>
                      </li>
                    ))}
                  </ol>
                ) : !activityLoading ? (
                  <p className="mt-3 text-sm text-slate-500">No run activity is available yet.</p>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {activeTab === "issues" ? (
        <div aria-labelledby="run-tab-issues" className="workspace-section grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]" id="run-panel-issues" role="tabpanel" tabIndex={0}>
          <div className="workspace-card">
            <div>
              <h2 className="text-lg font-semibold">Issue Log</h2>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Filter ingest warnings and errors, then open an entry to inspect the exact host, share, and path context.
              </p>
            </div>

            <div className="mt-4 grid gap-3">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                Search issue text
                <input
                  className="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  placeholder="Code, message, endpoint, share, or path"
                  value={issueSearch}
                  onChange={(event) => setIssueSearch(event.target.value)}
                />
              </label>
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                Severity
                <select
                  className="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-3 py-3 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={issueSeverity}
                  onChange={(event) => setIssueSeverity(event.target.value as RunIssueSeverity)}
                >
                  <option value="all">All severities</option>
                  <option value="error">Errors only</option>
                  <option value="warn">Warnings only</option>
                </select>
              </label>
            </div>

            <div className="mt-4 flex items-center gap-2">
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveBack(setIssueCursor, setIssueHistory)}
                disabled={issueHistory.length === 0}
                type="button"
              >
                Prev
              </button>
              <button
                className="rounded-2xl border border-slate-300 px-3 py-2 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
                onClick={() => moveCursor(issueNext, issueCursor, setIssueCursor, setIssueHistory)}
                disabled={!issueNext}
                type="button"
              >
                Next
              </button>
            </div>

            {issuesError ? (
              <div className="mt-4">
                <StatusBanner tone="error" title="Issue Log Unavailable">
                  <p>{issuesError}</p>
                  <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">Retry issue log</button>
                </StatusBanner>
              </div>
            ) : null}

            {issuesLoading ? <p className="mt-4 text-sm text-slate-500">Loading recorded issues.</p> : null}
            {!issuesLoading && issues.length === 0 ? (
              <div className="mt-4">
                <StatePanel
                  title="No Issues In View"
                  description={
                    (run?.summary?.errors || 0) > 0
                      ? "No issues match the current filter. Clear the search or severity filter to broaden the view."
                      : run?.status === "INGESTING"
                        ? "The worker is still ingesting, but no warnings or errors have been recorded yet."
                        : "No warnings or errors were recorded for this run."
                  }
                />
              </div>
            ) : null}

            <ul className="mt-4 max-h-[560px] space-y-2 overflow-auto">
              {issues.map((issue) => (
                <li key={issue.id}>
                  <button
                    className={`w-full rounded-2xl border px-4 py-3 text-left transition ${
                      selectedIssueId === issue.id
                        ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                        : "border-slate-300 bg-white hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-950/40 dark:hover:bg-slate-900/80"
                    }`}
                    onClick={() => setSelectedIssueId(issue.id)}
                    type="button"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] ${issueSeverityTone(issue.severity)}`}>
                        {issue.severity}
                      </span>
                      <span className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">{issue.code}</span>
                    </div>
                    <p className="mt-2 text-sm font-semibold">{issue.message}</p>
                    <div className="mt-2 space-y-1 text-xs text-slate-500">
                      {issue.endpoint_key ? <p>Host: {issue.endpoint_key}</p> : null}
                      {issue.resource_name ? <p>Share: {issue.resource_name}</p> : null}
                      {issue.path ? <p className="font-mono">{issue.path}</p> : null}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="workspace-card">
            {selectedIssue ? (
              <div className="space-y-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Selected Issue</p>
                    <h2 className="mt-2 text-2xl font-semibold tracking-tight">{selectedIssue.code}</h2>
                  </div>
                  <span className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] ${issueSeverityTone(selectedIssue.severity)}`}>
                    {selectedIssue.severity}
                  </span>
                </div>

                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Message</p>
                  <p className="mt-2 text-sm text-slate-700 dark:text-slate-200">{selectedIssue.message}</p>
                </div>

                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Host</p>
                    <p className="mt-1 text-sm font-semibold">{selectedIssue.endpoint_key || "Unavailable"}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Share</p>
                    <p className="mt-1 text-sm font-semibold">{selectedIssue.resource_name || "Unavailable"}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Recorded</p>
                    <p className="mt-1 text-sm font-semibold">{new Date(selectedIssue.created_at).toLocaleString()}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Issue ID</p>
                    <p className="mt-1 text-sm font-semibold">{selectedIssue.id}</p>
                  </div>
                </div>

                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Path</p>
                  <p className="mt-2 break-all font-mono text-xs text-slate-600 dark:text-slate-300">{selectedIssue.path || "No path recorded"}</p>
                </div>

                <div className="flex flex-wrap gap-2">
                  {selectedIssue.endpoint_key ? (
                    <button
                      className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                      onClick={() => focusIssueEndpoint(selectedIssue)}
                      type="button"
                    >
                      Focus Host
                    </button>
                  ) : null}
                  <button
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    onClick={() => openIssueInSearch(selectedIssue)}
                    type="button"
                  >
                    Search Related Items
                  </button>
                  <button
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    onClick={() => setActiveTab("diff")}
                    type="button"
                  >
                    Compare With Baseline
                  </button>
                </div>
              </div>
            ) : (
              <StatePanel
                title="Select An Issue"
                description={
                  (run?.summary?.errors || 0) > 0
                    ? "Choose an issue from the list to inspect the recorded message and pivot into host or item review."
                    : "No ingest warnings or errors are currently available for this run."
                }
              />
            )}
          </div>
        </div>
      ) : null}

      {activeTab === "diff" ? (
        <div aria-labelledby="run-tab-diff" className="workspace-section space-y-4" id="run-panel-diff" role="tabpanel" tabIndex={0}>
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
        <div aria-labelledby="run-tab-explore" className="workspace-section grid gap-4 md:grid-cols-3" id="run-panel-explore" role="tabpanel" tabIndex={0}>
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
                  <div className="mt-1 flex flex-wrap gap-x-3 text-slate-500">
                    <span>{item.is_dir ? "directory" : "file"}</span>
                    {!item.is_dir && item.size_bytes != null ? <span>{formatBytes(item.size_bytes)}</span> : null}
                    {item.mtime ? <span>Modified {new Date(item.mtime).toLocaleString()}</span> : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {activeTab === "search" ? (
        <div aria-labelledby="run-tab-search" className="workspace-section space-y-4" id="run-panel-search" role="tabpanel" tabIndex={0}>
          <div className="workspace-card">
            <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Run-Scoped Search</h2>
                <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                  Search items across the full run and keep browser-local query/ext combinations for quick recall.
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
                    placeholder="Save local preset..."
                    value={savedQueryLabel}
                    onChange={(event) => setSavedQueryLabel(event.target.value)}
                  />
                  <button
                    className="rounded-2xl bg-slate-900 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                    onClick={saveCurrentQuery}
                    type="button"
                  >
                    Save Local
                  </button>
                </div>
              </div>
            </div>

            {savedQueries.length > 0 ? (
              <div className="mb-4">
                <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">Stored in this browser for this run only.</p>
                <div className="flex flex-wrap gap-2">
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
                  <div className="mt-1 flex flex-wrap gap-x-3 text-slate-500">
                    <span>resource_id: {item.resource_id ?? "-"}</span>
                    {!item.is_dir && item.size_bytes != null ? <span>{formatBytes(item.size_bytes)}</span> : null}
                    {item.mtime ? <span>Modified {new Date(item.mtime).toLocaleString()}</span> : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </section>
  );
}
