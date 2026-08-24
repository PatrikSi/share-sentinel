import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AccessCapabilityCell, type AccessCapabilities } from "@/components/access-capability-cell";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
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
type Resource = {
  id: number;
  name: string;
  access_level: string;
  access_capabilities: AccessCapabilities | null;
  remark: string | null;
  share_type: string;
};
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
  truncation?: {
    detail_limit: number;
    truncated: boolean;
    sections: {
      new_shares: boolean;
      disappeared_shares: boolean;
      item_churn: boolean;
    };
  };
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
const MAX_SAVED_QUERIES = 25;

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

function readInitialRunDetailTab(): RunDetailTab {
  if (typeof window === "undefined") return "overview";
  const candidate = new URLSearchParams(window.location.search).get("view");
  return RUN_DETAIL_TABS.includes(candidate as RunDetailTab) ? (candidate as RunDetailTab) : "overview";
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function parseSavedQueries(raw: string | null): SavedQuery[] {
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) throw new Error("Saved search data is not a list.");

  return parsed
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === "object")
    .filter(
      (entry) =>
        typeof entry.id === "string" &&
        typeof entry.label === "string" &&
        entry.label.trim().length > 0 &&
        typeof entry.q === "string" &&
        typeof entry.ext === "string",
    )
    .slice(-MAX_SAVED_QUERIES)
    .map((entry) => ({ id: entry.id as string, label: (entry.label as string).trim(), q: entry.q as string, ext: entry.ext as string }));
}

type CursorPagerProps = {
  label: string;
  page: number;
  canPrevious: boolean;
  canNext: boolean;
  busy: boolean;
  onPrevious: () => void;
  onNext: () => void;
};

function CursorPager({ label, page, canPrevious, canNext, busy, onPrevious, onNext }: CursorPagerProps) {
  return (
    <nav aria-label={`${label} pagination`} className="mb-3 flex items-center gap-2">
      <span aria-live="polite" className="mr-auto text-xs text-slate-500">
        Page {page}
      </span>
      <button
        className="rounded-md border border-slate-300 px-3 py-2 text-xs font-semibold disabled:opacity-50 dark:border-slate-700"
        disabled={!canPrevious || busy}
        onClick={onPrevious}
        type="button"
      >
        Previous
      </button>
      <button
        className="rounded-md border border-slate-300 px-3 py-2 text-xs font-semibold disabled:opacity-50 dark:border-slate-700"
        disabled={!canNext || busy}
        onClick={onNext}
        type="button"
      >
        Next
      </button>
    </nav>
  );
}

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
    case "INGEST_PAUSED":
      return "Worker paused ingest safely";
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

  if (event.action === "INGEST_PAUSED") {
    const lineOffset = Number(event.metadata.line_offset || 0);
    const worker = typeof event.metadata.worker === "string" ? event.metadata.worker : "worker";
    return `${worker} checkpointed at line ${lineOffset.toLocaleString()} during shutdown. Another worker can resume the run.`;
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
  const [, setSearchParams] = useSearchParams();
  const session = useSession();
  const lastEndpointRequestKey = useRef<string | null>(null);
  const lastResourceRequestKey = useRef<string | null>(null);
  const lastItemRequestKey = useRef<string | null>(null);
  const lastGlobalSearchRequestKey = useRef<string | null>(null);

  const [run, setRun] = useState<RunInfo | null>(null);
  const [projectRuns, setProjectRuns] = useState<RunCompareOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshWarning, setRefreshWarning] = useState<string | null>(null);
  const [baselineOptionsError, setBaselineOptionsError] = useState<string | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [selectedBaselineRunId, setSelectedBaselineRunId] = useState("");
  const [runDiff, setRunDiff] = useState<RunDiffResult | null>(null);
  const [activeTab, setActiveTab] = useState<RunDetailTab>(readInitialRunDetailTab);

  const [endpointSearch, setEndpointSearch] = useState("");
  const [itemSearch, setItemSearch] = useState("");
  const [pathPrefix, setPathPrefix] = useState("");
  const [globalQuery, setGlobalQuery] = useState("");
  const [globalExt, setGlobalExt] = useState("");

  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [resources, setResources] = useState<Resource[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [globalItems, setGlobalItems] = useState<Item[]>([]);
  const [endpointsLoading, setEndpointsLoading] = useState(false);
  const [resourcesLoading, setResourcesLoading] = useState(false);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [globalItemsLoading, setGlobalItemsLoading] = useState(false);
  const [endpointsError, setEndpointsError] = useState<string | null>(null);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [itemsError, setItemsError] = useState<string | null>(null);
  const [globalItemsError, setGlobalItemsError] = useState<string | null>(null);

  const [selectedEndpoint, setSelectedEndpoint] = useState<number | null>(null);
  const [selectedResource, setSelectedResource] = useState<number | null>(null);

  const [endpointCursor, setEndpointCursor] = useState<string | null>(null);
  const [endpointHistory, setEndpointHistory] = useState<Array<string | null>>([]);
  const [endpointNext, setEndpointNext] = useState<string | null>(null);

  const [resourceCursor, setResourceCursor] = useState<string | null>(null);
  const [resourceHistory, setResourceHistory] = useState<Array<string | null>>([]);
  const [resourceNext, setResourceNext] = useState<string | null>(null);

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
  const [savedQueryError, setSavedQueryError] = useState<string | null>(null);
  const debouncedEndpointSearch = useDebouncedValue(endpointSearch, 300);
  const debouncedItemSearch = useDebouncedValue(itemSearch, 300);
  const debouncedPathPrefix = useDebouncedValue(pathPrefix, 300);
  const debouncedGlobalQuery = useDebouncedValue(globalQuery, 300);
  const debouncedGlobalExt = useDebouncedValue(globalExt, 300);
  const debouncedIssueSearch = useDebouncedValue(issueSearch, 300);

  useEffect(() => {
    if (!runId) return;
    try {
      setSavedQueries(parseSavedQueries(localStorage.getItem(savedQueriesKey)));
      setSavedQueryError(null);
    } catch (err) {
      setSavedQueries([]);
      setSavedQueryError(
        `${err instanceof Error ? err.message : "Saved search data could not be read."} Browser-local presets were ignored; you can create new ones.`,
      );
    }
  }, [runId, savedQueriesKey]);

  function persistSavedQueries(next: SavedQuery[]): boolean {
    const bounded = next.slice(-MAX_SAVED_QUERIES);
    try {
      localStorage.setItem(savedQueriesKey, JSON.stringify(bounded));
      setSavedQueries(bounded);
      setSavedQueryError(null);
      return true;
    } catch (err) {
      setSavedQueryError(
        `${err instanceof Error ? err.message : "Browser storage is unavailable."} The preset was not saved; keep this tab open or copy the query before leaving.`,
      );
      return false;
    }
  }

  useEffect(() => {
    const next = new URLSearchParams(window.location.search);
    if (activeTab === "overview") next.delete("view");
    else next.set("view", activeTab);
    setSearchParams(next, { replace: true });
  }, [activeTab, setSearchParams]);

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
    setBaselineOptionsError(null);
    apiFetch(`/projects/${projectId}/runs?limit=200`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setProjectRuns((data?.items || []) as RunCompareOption[]);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setBaselineOptionsError(
            err instanceof Error ? err.message : "Recent baseline run choices could not be loaded.",
          );
        }
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
    setResourceCursor(null);
    setResourceHistory([]);
    setResourceNext(null);
    setItemCursor(null);
    setItemHistory([]);
    setItemNext(null);
    setGlobalCursor(null);
    setGlobalHistory([]);
    setGlobalNext(null);
    setEndpointsLoading(false);
    setResourcesLoading(false);
    setItemsLoading(false);
    setGlobalItemsLoading(false);
    setEndpointsError(null);
    setResourcesError(null);
    setItemsError(null);
    setGlobalItemsError(null);
    lastEndpointRequestKey.current = null;
    lastResourceRequestKey.current = null;
    lastItemRequestKey.current = null;
    lastGlobalSearchRequestKey.current = null;
    setActiveTab(readInitialRunDetailTab());
  }, [projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || !run) return;
    if (run.status !== "COMPLETE") {
      setDiffLoading(false);
      setDiffError(null);
      setRunDiff(null);
      return;
    }
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
  }, [projectId, reloadNonce, run?.status, runId, selectedBaselineRunId]);

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
        if (!stopped && !tickController.signal.aborted) {
          setRun(data as RunInfo);
          setRefreshWarning(null);
        }
      } catch (err) {
        if (!stopped && !tickController.signal.aborted && !isAbortError(err)) {
          setRefreshWarning(
            `Live run refresh is delayed; showing the last confirmed state. ${err instanceof Error ? err.message : "Retry when the API is available."}`,
          );
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
  }, [debouncedIssueSearch, issueSeverity, projectId, runId]);

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
    if (debouncedIssueSearch.trim()) query.set("search", debouncedIssueSearch.trim());
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
  }, [projectId, reloadNonce, runId, debouncedIssueSearch, issueSeverity, issueCursor, activeTab, run?.summary?.errors, run?.status]);

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
  }, [debouncedEndpointSearch, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || activeTab !== "explore") {
      setEndpointsLoading(false);
      return;
    }
    const requestKey = JSON.stringify([projectId, runId, debouncedEndpointSearch, endpointCursor, reloadNonce]);
    if (lastEndpointRequestKey.current === requestKey) return;
    const query = new URLSearchParams({ limit: "100", search: debouncedEndpointSearch });
    if (endpointCursor) query.set("cursor", endpointCursor);

    const controller = new AbortController();
    setEndpointsLoading(true);
    setEndpointsError(null);
    setEndpoints([]);
    setSelectedEndpoint(null);
    setResourceCursor(null);
    setResourceHistory([]);
    setResourceNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const rows = (data?.items || []) as Endpoint[];
        lastEndpointRequestKey.current = requestKey;
        setEndpoints(rows);
        setEndpointNext((data?.next_cursor as string | null) || null);
        setSelectedEndpoint(rows[0]?.id || null);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setEndpointsError(err instanceof Error ? err.message : "Endpoint inventory could not be loaded.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setEndpointsLoading(false);
      });
    return () => controller.abort();
  }, [activeTab, projectId, reloadNonce, runId, debouncedEndpointSearch, endpointCursor]);

  useEffect(() => {
    if (!projectId || !runId || !selectedEndpoint || activeTab !== "explore") {
      if (activeTab !== "explore" && selectedEndpoint) {
        setResourcesLoading(false);
        return;
      }
      setResources([]);
      setSelectedResource(null);
      setResourceNext(null);
      setResourcesLoading(false);
      setResourcesError(null);
      return;
    }

    const controller = new AbortController();
    const query = new URLSearchParams({ limit: "200" });
    if (resourceCursor) query.set("cursor", resourceCursor);
    const requestKey = JSON.stringify([projectId, runId, selectedEndpoint, resourceCursor, reloadNonce]);
    if (lastResourceRequestKey.current === requestKey) return;
    setResourcesLoading(true);
    setResourcesError(null);
    setResources([]);
    setSelectedResource(null);
    setResourceNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const rows = (data?.items || []) as Resource[];
        lastResourceRequestKey.current = requestKey;
        setResources(rows);
        setResourceNext((data?.next_cursor as string | null) || null);
        setSelectedResource((current) => {
          if (current && rows.some((resource) => resource.id === current)) {
            return current;
          }
          return rows[0]?.id || null;
        });
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setResourcesError(err instanceof Error ? err.message : "Share inventory could not be loaded.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setResourcesLoading(false);
      });
    return () => controller.abort();
  }, [activeTab, projectId, reloadNonce, resourceCursor, runId, selectedEndpoint]);

  useEffect(() => {
    setItemCursor(null);
    setItemHistory([]);
  }, [selectedResource, debouncedItemSearch, debouncedPathPrefix, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || !selectedResource || activeTab !== "explore") {
      if (activeTab !== "explore" && selectedResource) {
        setItemsLoading(false);
        return;
      }
      setItems([]);
      setItemNext(null);
      setItemsLoading(false);
      setItemsError(null);
      return;
    }

    const query = new URLSearchParams({ limit: "200", search: debouncedItemSearch });
    if (debouncedPathPrefix.trim()) query.set("path_prefix", debouncedPathPrefix.trim());
    if (itemCursor) query.set("cursor", itemCursor);
    const requestKey = JSON.stringify([
      projectId,
      runId,
      selectedResource,
      debouncedItemSearch,
      debouncedPathPrefix,
      itemCursor,
      reloadNonce,
    ]);
    if (lastItemRequestKey.current === requestKey) return;

    const controller = new AbortController();
    setItemsLoading(true);
    setItemsError(null);
    setItems([]);
    setItemNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/resources/${selectedResource}/items?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        lastItemRequestKey.current = requestKey;
        setItems((data?.items || []) as Item[]);
        setItemNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setItemsError(err instanceof Error ? err.message : "Items could not be loaded for this share.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setItemsLoading(false);
      });
    return () => controller.abort();
  }, [activeTab, projectId, reloadNonce, runId, selectedResource, debouncedItemSearch, debouncedPathPrefix, itemCursor]);

  useEffect(() => {
    setGlobalCursor(null);
    setGlobalHistory([]);
  }, [debouncedGlobalQuery, debouncedGlobalExt, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || activeTab !== "search") {
      setGlobalItemsLoading(false);
      return;
    }
    const query = new URLSearchParams({ limit: "200", q: debouncedGlobalQuery });
    if (debouncedGlobalExt) query.set("ext", debouncedGlobalExt);
    if (globalCursor) query.set("cursor", globalCursor);
    const requestKey = JSON.stringify([projectId, runId, debouncedGlobalQuery, debouncedGlobalExt, globalCursor, reloadNonce]);
    if (lastGlobalSearchRequestKey.current === requestKey) return;

    const controller = new AbortController();
    setGlobalItemsLoading(true);
    setGlobalItemsError(null);
    setGlobalItems([]);
    setGlobalNext(null);
    apiFetch(`/projects/${projectId}/runs/${runId}/search/items?${query.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        lastGlobalSearchRequestKey.current = requestKey;
        setGlobalItems((data?.items || []) as Item[]);
        setGlobalNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setGlobalItemsError(err instanceof Error ? err.message : "Run-scoped item search could not be loaded.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setGlobalItemsLoading(false);
      });
    return () => controller.abort();
  }, [activeTab, projectId, reloadNonce, runId, debouncedGlobalQuery, debouncedGlobalExt, globalCursor]);

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

  function selectEndpoint(endpointId: number) {
    if (selectedEndpoint === endpointId) return;
    setSelectedEndpoint(endpointId);
    setResourceCursor(null);
    setResourceHistory([]);
    setResourceNext(null);
  }

  function saveCurrentQuery() {
    const label = savedQueryLabel.trim();
    if (!label) {
      setSavedQueryError("Enter a preset name before saving this search.");
      return;
    }
    if (!globalQuery.trim() && !globalExt.trim()) {
      setSavedQueryError("Enter a query or extension before saving a preset.");
      return;
    }
    const next: SavedQuery[] = [
      ...savedQueries.filter((query) => query.label.toLocaleLowerCase() !== label.toLocaleLowerCase()),
      {
        id: crypto.randomUUID(),
        label,
        q: globalQuery,
        ext: globalExt,
      },
    ];
    if (persistSavedQueries(next)) {
      setSavedQueryLabel("");
    }
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
    setRefreshWarning(null);
    setBaselineOptionsError(null);
    setDiffError(null);
    setIssuesError(null);
    setActivityError(null);
    setEndpointsError(null);
    setResourcesError(null);
    setItemsError(null);
    setGlobalItemsError(null);
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
  const issuesBusy = issuesLoading || issueSearch !== debouncedIssueSearch;
  const endpointsBusy = endpointsLoading || endpointSearch !== debouncedEndpointSearch;
  const itemsBusy =
    itemsLoading || itemSearch !== debouncedItemSearch || pathPrefix !== debouncedPathPrefix;
  const globalItemsBusy =
    globalItemsLoading || globalQuery !== debouncedGlobalQuery || globalExt !== debouncedGlobalExt;

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
        {refreshWarning ? (
          <StatusBanner tone="warning" title="Live run state may be stale">
            <p>{refreshWarning}</p>
            <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
              Retry live state
            </button>
          </StatusBanner>
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
          <div aria-busy={issuesBusy} className="workspace-card">
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

            <div className="mt-4">
              <CursorPager
                busy={issuesBusy}
                canNext={!!issueNext}
                canPrevious={issueHistory.length > 0}
                label="Issue log"
                onNext={() => moveCursor(issueNext, issueCursor, setIssueCursor, setIssueHistory)}
                onPrevious={() => moveBack(setIssueCursor, setIssueHistory)}
                page={issueHistory.length + 1}
              />
            </div>

            {issuesError ? (
              <div className="mt-4">
                <StatusBanner tone="error" title="Issue Log Unavailable">
                  <p>{issuesError}</p>
                  <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">Retry issue log</button>
                </StatusBanner>
              </div>
            ) : null}

            {issuesBusy ? <p className="mt-4 text-sm text-slate-500" role="status">Updating recorded issues…</p> : null}
            {!issuesBusy && !issuesError && issues.length === 0 ? (
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
                  aria-describedby={baselineOptionsError ? "baseline-options-error" : "baseline-options-help"}
                  className="mt-1 w-full rounded-2xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  disabled={run?.status !== "COMPLETE"}
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
                <span className="mt-1 block text-[11px] font-normal normal-case tracking-normal text-slate-500" id="baseline-options-help">
                  Bounded to the 200 most recent runs. Automatic comparison still chooses the nearest prior complete run.
                </span>
              </label>
            </div>

            {baselineOptionsError ? (
              <div className="mt-3" id="baseline-options-error">
                <StatusBanner tone="warning" title="Recent baseline choices unavailable">
                  <p>{baselineOptionsError}</p>
                  <p className="mt-1">Automatic nearest-baseline comparison remains available. Retry to restore the manual selector.</p>
                  <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                    Retry baseline choices
                  </button>
                </StatusBanner>
              </div>
            ) : null}

            {run && run.status !== "COMPLETE" ? (
              <div className="mt-3">
                {run.status === "UPLOADED" || run.status === "INGESTING" ? (
                  <StatusBanner tone="warning" title="Comparison waits for active ingest">
                    <p>
                      This run is {run.status.toLowerCase().replaceAll("_", " ")}. Diff totals would be incomplete, so this page will keep checking and load the comparison after ingestion completes.
                    </p>
                  </StatusBanner>
                ) : run.status === "PENDING_UPLOAD" ? (
                  <StatusBanner tone="info" title="Upload required before comparison">
                    <p>This run has no accepted artifact yet. Upload an artifact and complete ingestion before a run-to-run comparison is available.</p>
                  </StatusBanner>
                ) : run.status === "FAILED" ? (
                  <StatusBanner tone="error" title="Comparison unavailable for failed run">
                    <p>Ingestion did not complete, so no trustworthy diff can be generated. Review the recorded issue and submit a corrected artifact as a new run.</p>
                  </StatusBanner>
                ) : (
                  <StatusBanner tone="info" title="Comparison is not available">
                    <p>This run is {run.status.toLowerCase().replaceAll("_", " ")}. A comparison is available only after the run reaches complete.</p>
                  </StatusBanner>
                )}
              </div>
            ) : null}

            {diffLoading ? <p className="mt-3 text-sm text-slate-500">Loading run diff.</p> : null}
            {diffError ? (
              <div className="mt-3">
                <StatusBanner tone="error" title="Run comparison unavailable">
                  <p>{diffError}</p>
                  <p className="mt-1">No comparison result is being shown. Retrying this read is safe.</p>
                  <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                    Retry comparison
                  </button>
                </StatusBanner>
              </div>
            ) : null}

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
                  {runDiff.truncation?.truncated ? (
                    <StatusBanner tone="warning" title="Detail lists are truncated">
                      <p>
                        Summary totals are exact. Each affected detail section shows at most {runDiff.truncation.detail_limit.toLocaleString()} records to keep this comparison responsive.
                      </p>
                    </StatusBanner>
                  ) : null}
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
        <div
          aria-labelledby="run-tab-explore"
          className="workspace-section grid gap-4 xl:grid-cols-[minmax(250px,0.8fr)_minmax(280px,1fr)_minmax(340px,1.3fr)]"
          id="run-panel-explore"
          role="tabpanel"
          tabIndex={0}
        >
          <div aria-busy={endpointsBusy} className="workspace-card">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <h2 className="text-lg font-semibold">Endpoints</h2>
                <p className="mt-1 text-xs text-slate-500">Choose a host to load its shares.</p>
              </div>
              <input
                aria-label="Search endpoints in this run"
                className="w-44 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search endpoint"
                value={endpointSearch}
                onChange={(event) => setEndpointSearch(event.target.value)}
              />
            </div>
            <CursorPager
              busy={endpointsBusy}
              canNext={!!endpointNext}
              canPrevious={endpointHistory.length > 0}
              label="Endpoint inventory"
              onNext={() => moveCursor(endpointNext, endpointCursor, setEndpointCursor, setEndpointHistory)}
              onPrevious={() => moveBack(setEndpointCursor, setEndpointHistory)}
              page={endpointHistory.length + 1}
            />
            {endpointsError ? (
              <StatusBanner tone="error" title="Endpoints unavailable">
                <p>{endpointsError}</p>
                <p className="mt-1">No endpoint result is being shown. Retrying is safe.</p>
                <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                  Retry endpoints
                </button>
              </StatusBanner>
            ) : null}
            {endpointsBusy ? <p className="text-sm text-slate-500" role="status">Updating endpoint inventory…</p> : null}
            {!endpointsBusy && !endpointsError && endpoints.length === 0 ? <p className="text-sm text-slate-500">No endpoints match this run search.</p> : null}
            <ul className="max-h-[520px] space-y-2 overflow-auto">
              {endpoints.map((endpoint) => (
                <li key={endpoint.id}>
                  <button
                    className={`w-full rounded-2xl border px-3 py-3 text-left text-xs ${
                      selectedEndpoint === endpoint.id
                        ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                        : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    }`}
                    onClick={() => selectEndpoint(endpoint.id)}
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

          <div aria-busy={resourcesLoading} className="workspace-card">
            <div className="mb-3">
              <h2 className="text-lg font-semibold">Shares</h2>
              <p className="mt-1 text-xs text-slate-500">Select a share to inspect items in that branch.</p>
            </div>
            <CursorPager
              busy={resourcesLoading}
              canNext={!!resourceNext}
              canPrevious={resourceHistory.length > 0}
              label="Share inventory"
              onNext={() => moveCursor(resourceNext, resourceCursor, setResourceCursor, setResourceHistory)}
              onPrevious={() => moveBack(setResourceCursor, setResourceHistory)}
              page={resourceHistory.length + 1}
            />
            {resourcesError ? (
              <StatusBanner tone="error" title="Shares unavailable">
                <p>{resourcesError}</p>
                <p className="mt-1">No share result is being shown. Retrying is safe.</p>
                <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                  Retry shares
                </button>
              </StatusBanner>
            ) : null}
            {resourcesLoading ? <p className="text-sm text-slate-500" role="status">Loading shares for the selected endpoint…</p> : null}
            {!selectedEndpoint && !endpointsBusy && !endpointsError ? <p className="text-sm text-slate-500">Select an endpoint to inspect its shares.</p> : null}
            {selectedEndpoint && !resourcesLoading && !resourcesError && resources.length === 0 ? <p className="text-sm text-slate-500">No shares were returned for the selected endpoint.</p> : null}
            <ul className="max-h-[520px] space-y-2 overflow-auto">
              {resources.map((resource) => (
                <li
                  className={`overflow-hidden rounded-2xl border text-xs ${
                    selectedResource === resource.id
                      ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20"
                      : "border-slate-300 dark:border-slate-700"
                  }`}
                  key={resource.id}
                >
                  <button
                    className="w-full px-3 py-3 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                    onClick={() => setSelectedResource(resource.id)}
                    type="button"
                  >
                    <span className="block font-semibold">{resource.name}</span>
                    <span className="mt-1 block text-slate-500">{resource.share_type.toUpperCase()}</span>
                    {resource.remark ? <span className="mt-1 block text-slate-500">{resource.remark}</span> : null}
                  </button>
                  <div className="border-t border-slate-200 bg-white/70 px-3 py-2 dark:border-slate-700 dark:bg-slate-950/30">
                    <AccessCapabilityCell
                      accessLevel={resource.access_level}
                      capabilities={resource.access_capabilities}
                      label="Share access"
                    />
                  </div>
                </li>
              ))}
            </ul>
          </div>

          <div aria-busy={itemsBusy} className="workspace-card">
            <div className="mb-3 grid gap-2">
              <div>
                <h2 className="text-lg font-semibold">Items</h2>
                <p className="mt-1 text-xs text-slate-500">Filter the selected share by name or path prefix.</p>
              </div>
              <input
                aria-label="Search item names in the selected share"
                className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search name"
                value={itemSearch}
                onChange={(event) => setItemSearch(event.target.value)}
              />
              <input
                aria-label="Filter selected share by path prefix"
                className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Path prefix (e.g. \\HR\\)"
                value={pathPrefix}
                onChange={(event) => setPathPrefix(event.target.value)}
              />
            </div>
            <CursorPager
              busy={itemsBusy}
              canNext={!!itemNext}
              canPrevious={itemHistory.length > 0}
              label="Share items"
              onNext={() => moveCursor(itemNext, itemCursor, setItemCursor, setItemHistory)}
              onPrevious={() => moveBack(setItemCursor, setItemHistory)}
              page={itemHistory.length + 1}
            />
            {itemsError ? (
              <StatusBanner tone="error" title="Items unavailable">
                <p>{itemsError}</p>
                <p className="mt-1">No item result is being shown. Retrying is safe.</p>
                <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                  Retry items
                </button>
              </StatusBanner>
            ) : null}
            {itemsBusy ? <p className="text-sm text-slate-500" role="status">Updating items in the selected share…</p> : null}
            {!selectedResource && !endpointsBusy && !endpointsError && !resourcesLoading && !resourcesError ? <p className="text-sm text-slate-500">Select a share to inspect its files and folders.</p> : null}
            {selectedResource && !itemsBusy && !itemsError && items.length === 0 ? <p className="text-sm text-slate-500">No items match the current share filters.</p> : null}
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
          <div aria-busy={globalItemsBusy} className="workspace-card">
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
                    aria-label="Search items across this run"
                    className="rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder="Query"
                    value={globalQuery}
                    onChange={(event) => setGlobalQuery(event.target.value)}
                  />
                </div>
                <div>
                  <input
                    aria-label="Filter run search by file extension"
                    className="w-24 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder=".ext"
                    value={globalExt}
                    onChange={(event) => setGlobalExt(event.target.value)}
                  />
                </div>
                <div className="flex items-center gap-2">
                  <input
                    aria-describedby={savedQueryError ? "saved-query-error" : undefined}
                    aria-label="Name this browser-local search preset"
                    className="w-36 rounded-2xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
                    placeholder="Save local preset..."
                    value={savedQueryLabel}
                    onChange={(event) => {
                      setSavedQueryLabel(event.target.value);
                      setSavedQueryError(null);
                    }}
                  />
                  <button
                    className="rounded-2xl bg-slate-900 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                    onClick={saveCurrentQuery}
                    type="button"
                  >
                    Save preset
                  </button>
                </div>
              </div>
            </div>

            {savedQueryError ? (
              <div className="mb-4">
                <StatusBanner tone="error" title="Preset not available">
                  <p id="saved-query-error">{savedQueryError}</p>
                </StatusBanner>
              </div>
            ) : null}

            {savedQueries.length > 0 ? (
              <div className="mb-4">
                <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
                  Stored in this browser for this run only. Up to {MAX_SAVED_QUERIES} presets are retained.
                </p>
                <div className="flex flex-wrap gap-2">
                  {savedQueries.map((saved) => (
                    <div key={saved.id} className="flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-xs dark:bg-slate-800">
                      <button
                        className="font-semibold"
                        onClick={() => {
                          setGlobalQuery(saved.q);
                          setGlobalExt(saved.ext);
                          setSavedQueryError(null);
                        }}
                        type="button"
                      >
                        {saved.label}
                      </button>
                      <button
                        aria-label={`Remove saved preset ${saved.label}`}
                        className="text-slate-500"
                        onClick={() => removeSavedQuery(saved.id)}
                        type="button"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <CursorPager
              busy={globalItemsBusy}
              canNext={!!globalNext}
              canPrevious={globalHistory.length > 0}
              label="Run search"
              onNext={() => moveCursor(globalNext, globalCursor, setGlobalCursor, setGlobalHistory)}
              onPrevious={() => moveBack(setGlobalCursor, setGlobalHistory)}
              page={globalHistory.length + 1}
            />

            {globalItemsError ? (
              <StatusBanner tone="error" title="Run search unavailable">
                <p>{globalItemsError}</p>
                <p className="mt-1">No search result is being shown. Retrying is safe.</p>
                <button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={retryRunData} type="button">
                  Retry search
                </button>
              </StatusBanner>
            ) : null}
            {globalItemsBusy ? <p className="text-sm text-slate-500" role="status">Updating run search…</p> : null}
            {!globalItemsBusy && !globalItemsError && globalItems.length === 0 ? <p className="text-sm text-slate-500">No run-scoped search results match the current query.</p> : null}
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
