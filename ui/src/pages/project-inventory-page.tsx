import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AccessCapabilityCell, type AccessCapabilities } from "@/components/access-capability-cell";
import { ColumnPicker } from "@/components/column-picker";
import { Dialog } from "@/components/dialog";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { parseInventoryQuery, type InventoryQueryClause, type InventoryQueryField, type InventoryQueryGroup } from "@/lib/inventory-query";

type Project = { id: string; name: string };
type RunOption = { id: string; name: string; status: string; created_at: string };
type ExtensionFacet = { ext: string; count: number };
type InventoryStats = { runs_ingesting?: number };
type ProjectRoleStatus = "loading" | "ready" | "error";

type InventoryItem = {
  id: number;
  run_id: string;
  run_name: string;
  endpoint_key: string;
  hostname: string | null;
  ip: string | null;
  resource_name: string;
  access_level: string;
  access_capabilities: AccessCapabilities | null;
  share_type: string;
  path: string;
  name: string;
  is_dir: boolean;
  size_bytes?: number | null;
  allocation_size_bytes?: number | null;
  mtime?: string | null;
  created_at?: string | null;
  accessed_at?: string | null;
  changed_at?: string | null;
  file_attributes?: string[] | null;
};

type InventoryResource = {
  id: number;
  run_id: string;
  run_name: string;
  endpoint_key: string;
  hostname: string | null;
  name: string;
  remark: string | null;
  access_level: string;
  access_capabilities: AccessCapabilities | null;
  share_type: string;
  item_count: number;
};

type InventoryEndpoint = {
  id: number;
  run_id: string;
  run_name: string;
  endpoint_key: string;
  ip: string | null;
  hostname: string | null;
  domain: string | null;
  smb_signing: string | null;
  resource_count: number;
  item_count: number;
};

type Tab = "items" | "resources" | "endpoints";
type SavedInvestigationDefinition = {
  active_tab?: Tab;
  selected_run_ids?: string[];
  filters?: {
    query?: string;
    endpoint_filter?: string;
    share_filter?: string;
    path_prefix?: string;
    ext_filter?: string;
    resource_access?: string;
  };
  applied_query?: string;
  draft_query?: string;
};
type SavedInvestigation = {
  id: string;
  project_id: string;
  created_by_user_id: string | null;
  name: string;
  description: string | null;
  target_tab: Tab;
  query_text: string;
  definition: SavedInvestigationDefinition;
  created_at: string;
  updated_at: string;
};
type ItemColumnKey =
  | "path"
  | "name"
  | "resource_name"
  | "share_type"
  | "access_level"
  | "endpoint_key"
  | "hostname"
  | "ip"
  | "run_name"
  | "run_id"
  | "is_dir"
  | "size_bytes"
  | "allocation_size_bytes"
  | "mtime"
  | "created_at"
  | "accessed_at"
  | "changed_at"
  | "file_attributes";
type ResourceColumnKey = "name" | "share_type" | "access_level" | "endpoint_key" | "hostname" | "item_count" | "run_name" | "run_id" | "remark";
type EndpointColumnKey = "endpoint_key" | "hostname" | "ip" | "domain" | "smb_signing" | "resource_count" | "item_count" | "run_name" | "run_id";
type QueryFilterReflection = {
  value: string;
  modeLabel: string | null;
  summary: string | null;
  selectValue: string;
};
type Density = "compact" | "comfortable";
type CellFilterField = InventoryQueryField | null;

const DEFAULT_ITEM_COLUMNS: ItemColumnKey[] = ["path", "name", "resource_name", "access_level", "hostname", "size_bytes", "run_name"];
const DEFAULT_RESOURCE_COLUMNS: ResourceColumnKey[] = ["name", "share_type", "access_level", "hostname", "item_count", "run_name", "remark"];
const DEFAULT_ENDPOINT_COLUMNS: EndpointColumnKey[] = ["endpoint_key", "hostname", "ip", "domain", "resource_count", "item_count", "run_name"];

const ITEM_COLUMN_OPTIONS: Array<{ key: ItemColumnKey; label: string }> = [
  { key: "path", label: "Path" },
  { key: "name", label: "Name" },
  { key: "resource_name", label: "Share" },
  { key: "share_type", label: "Share Type" },
  { key: "access_level", label: "Observed Access" },
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Hostname" },
  { key: "ip", label: "IP" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
  { key: "is_dir", label: "Type" },
  { key: "size_bytes", label: "Size" },
  { key: "allocation_size_bytes", label: "Allocated Size" },
  { key: "mtime", label: "Modified" },
  { key: "created_at", label: "Created" },
  { key: "accessed_at", label: "Last Accessed" },
  { key: "changed_at", label: "Metadata Changed" },
  { key: "file_attributes", label: "Attributes" },
];
const RESOURCE_COLUMN_OPTIONS: Array<{ key: ResourceColumnKey; label: string }> = [
  { key: "name", label: "Share" },
  { key: "share_type", label: "Share Type" },
  { key: "access_level", label: "Observed Access" },
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Hostname" },
  { key: "item_count", label: "Items" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
  { key: "remark", label: "Remark" },
];
const ENDPOINT_COLUMN_OPTIONS: Array<{ key: EndpointColumnKey; label: string }> = [
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Hostname" },
  { key: "ip", label: "IP" },
  { key: "domain", label: "Domain" },
  { key: "smb_signing", label: "Signing" },
  { key: "resource_count", label: "Shares" },
  { key: "item_count", label: "Items" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
];
const QUERYABLE_FIELDS: InventoryQueryField[] = ["search", "endpoint", "share", "path", "ext", "access"];
const ACCESS_QUERY_ALIASES: Record<string, string> = {
  no_access: "no_access",
  none: "no_access",
  denied: "no_access",
  access_denied: "no_access",
  list_only: "list_only",
  list: "list_only",
  browse: "list_only",
  list_observed: "list_only",
  readable: "readable",
  read: "readable",
  read_only: "readable",
  read_observed: "readable",
  read_write: "readable",
  "read-write": "readable",
  unknown: "unknown",
  inconclusive: "unknown",
  not_tested: "unknown",
};

function normalizeReflectionValue(field: InventoryQueryField, value: string): string {
  const trimmed = value.trim();
  if (field === "ext") {
    if (!trimmed) return "";
    return trimmed.startsWith(".") ? trimmed.toLowerCase() : `.${trimmed.toLowerCase()}`;
  }
  if (field === "access") {
    return ACCESS_QUERY_ALIASES[trimmed.toLowerCase().replaceAll(" ", "_")] || trimmed.toLowerCase();
  }
  return trimmed;
}

function formatClauseMode(clause: InventoryQueryClause): string {
  const operatorLabel = clause.operator === "equals" ? "equals" : clause.operator;
  return `${clause.negated ? "NOT " : ""}${operatorLabel}`;
}

function formatClauseSummary(clause: InventoryQueryClause): string {
  return `${formatClauseMode(clause)} ${normalizeReflectionValue(clause.field, clause.value)}`;
}

function buildQueryFilterReflection(groups: InventoryQueryGroup[], field: InventoryQueryField): QueryFilterReflection {
  const clauses = [
    ...new Map(
      groups
        .flatMap((group) => group.filter((clause) => clause.field === field))
        .map((clause) => [`${clause.field}:${clause.operator}:${clause.negated}:${normalizeReflectionValue(field, clause.value)}`, clause]),
    ).values(),
  ];
  if (clauses.length === 0) {
    return { value: "", modeLabel: null, summary: null, selectValue: "" };
  }

  const normalizedValues = clauses.map((clause) => normalizeReflectionValue(field, clause.value));
  const modeLabel = clauses.length === 1 ? formatClauseMode(clauses[0]) : `${clauses.length} clauses`;
  const summary = clauses.map((clause) => formatClauseSummary(clause)).join(" OR ");
  const selectValue =
    clauses.length === 1 && !clauses[0].negated && clauses[0].operator === "equals" ? normalizedValues[0] : "";

  return {
    value: clauses.length === 1 ? normalizedValues[0] : normalizedValues.join(" | "),
    modeLabel,
    summary,
    selectValue,
  };
}

function blankQueryFilterReflections(): Record<InventoryQueryField, QueryFilterReflection> {
  return {
    search: { value: "", modeLabel: null, summary: null, selectValue: "" },
    endpoint: { value: "", modeLabel: null, summary: null, selectValue: "" },
    share: { value: "", modeLabel: null, summary: null, selectValue: "" },
    path: { value: "", modeLabel: null, summary: null, selectValue: "" },
    ext: { value: "", modeLabel: null, summary: null, selectValue: "" },
    access: { value: "", modeLabel: null, summary: null, selectValue: "" },
  };
}

function isTab(value: string): value is Tab {
  return value === "items" || value === "resources" || value === "endpoints";
}

const INVENTORY_TAB_COPY: Record<Tab, { label: string; description: string; emptyTitle: string; emptyBody: string }> = {
  items: {
    label: "Files & Folders",
    description: "Trace individual paths, file types, and storage hotspots across the selected project runs.",
    emptyTitle: "No files or folders match these filters.",
    emptyBody: "Broaden the guided filters, clear the run scope, or switch to the advanced query builder.",
  },
  resources: {
    label: "Shares",
    description: "Review exposed shares, access levels, and remarks before drilling into paths.",
    emptyTitle: "No shares match these filters.",
    emptyBody: "Try a broader endpoint or access filter, or compare a different run scope.",
  },
  endpoints: {
    label: "Endpoints",
    description: "Scan hosts in scope first, then pivot into the shares and items behind each endpoint.",
    emptyTitle: "No endpoints match these filters.",
    emptyBody: "Adjust the search terms or clear the run scope to widen the endpoint set.",
  },
};

const QUERY_FIELD_LABELS: Record<InventoryQueryField, string> = {
  search: "Search",
  endpoint: "Endpoint",
  share: "Share",
  path: "Path",
  ext: "Extension",
  access: "Observed Access",
};

const FILTER_LABEL_CLASS = "text-xs font-semibold uppercase tracking-wider text-slate-500";
const FILTER_INPUT_CLASS =
  "mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500";
const FILTER_SELECT_CLASS =
  "mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";

function readInitialSearchParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function readInitialTab(): Tab {
  const value = readInitialSearchParam("tab");
  return isTab(value) ? value : "items";
}

function readInitialRuns(): string[] {
  return readInitialSearchParam("runs")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function readStoredColumns<T extends string>(storageKey: string, options: Array<{ key: T }>, fallback: T[]): T[] {
  if (typeof window === "undefined") return fallback;
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
    if (!Array.isArray(parsed)) return fallback;
    const allowed = new Set(options.map((option) => option.key));
    const selected = parsed.filter((value): value is T => typeof value === "string" && allowed.has(value as T));
    return selected.length > 0 ? [...new Set(selected)] : fallback;
  } catch {
    return fallback;
  }
}

function quoteInventoryQueryValue(value: string): string | null {
  const normalized = value.replace(/[\r\n]+/g, " ").trim();
  if (!normalized) return null;
  if (!normalized.includes('"')) return `"${normalized}"`;
  if (!normalized.includes("'")) return `'${normalized}'`;
  return null;
}

function serializeInventoryClause(clause: InventoryQueryClause): string | null {
  const value = quoteInventoryQueryValue(clause.value);
  if (!value) return null;
  const operator = clause.operator === "equals" ? "=" : clause.operator === "contains" ? "~" : "^";
  return `${clause.negated ? "!" : ""}${clause.field}${operator}${value}`;
}

function serializeInventoryGroups(groups: InventoryQueryGroup[]): string | null {
  const serializedGroups: string[] = [];
  for (const group of groups) {
    const serializedClauses = group.map(serializeInventoryClause);
    if (serializedClauses.some((clause) => clause === null)) return null;
    if (serializedClauses.length > 0) serializedGroups.push(serializedClauses.join(" AND "));
  }
  return serializedGroups.join(" OR ");
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatFileAttributes(value: string[] | null | undefined): string {
  if (!value || value.length === 0) return "—";
  return value
    .map((attribute) => attribute.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase()))
    .join(", ");
}

type InventoryCellProps = {
  text: string;
  label: string;
  filterField?: CellFilterField;
  filterScopeLabel?: string;
  filterValue?: string;
  mono?: boolean;
  badge?: "neutral" | "positive" | "warning";
  onFilter: (field: InventoryQueryField, value: string, negated: boolean) => void;
  onCopy: (value: string, label: string) => void;
};

function InventoryCell({ text, label, filterField = null, filterScopeLabel, filterValue, mono = false, badge, onFilter, onCopy }: InventoryCellProps) {
  const exactValue = filterValue ?? text;
  const actionable = exactValue !== "" && exactValue !== "—";
  const filterTarget = filterScopeLabel || label;
  return (
    <div className="inventory-cell">
      <span
        className={`inventory-cell-text ${mono ? "is-mono" : ""} ${badge ? `inventory-value-badge is-${badge}` : ""}`}
        title={text === "—" ? undefined : text}
      >
        {text}
      </span>
      {actionable ? (
        <span className="inventory-cell-actions">
          {filterField ? (
            <>
              <button
                aria-label={`Filter all results where ${filterTarget} exactly matches ${exactValue}`}
                onClick={() => onFilter(filterField, exactValue, false)}
                title={`Filter all results: ${filterTarget} exact match`}
                type="button"
              >
                =
              </button>
              <button
                aria-label={`Exclude all results where ${filterTarget} exactly matches ${exactValue}`}
                onClick={() => onFilter(filterField, exactValue, true)}
                title={`Exclude all results: ${filterTarget} exact match`}
                type="button"
              >
                ≠
              </button>
            </>
          ) : null}
          <button aria-label={`Copy ${label}`} onClick={() => onCopy(exactValue, label)} title="Copy exact value" type="button">
            Copy
          </button>
        </span>
      ) : null}
    </div>
  );
}

export function ProjectInventoryPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [, setSearchParams] = useSearchParams();
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const copiedNoticeTimer = useRef<number | null>(null);
  const initialDsl = useRef(readInitialSearchParam("queryDsl"));
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;

  const [project, setProject] = useState<Project | null>(null);
  const [projectRole, setProjectRole] = useState<string | null>(null);
  const [projectRoleStatus, setProjectRoleStatus] = useState<ProjectRoleStatus>(projectId ? "loading" : "error");
  const [runs, setRuns] = useState<RunOption[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>(readInitialRuns);

  const [activeTab, setActiveTab] = useState<Tab>(readInitialTab);
  const [query, setQuery] = useState(() => readInitialSearchParam("q"));
  const [endpointFilter, setEndpointFilter] = useState(() => readInitialSearchParam("endpoint"));
  const [shareFilter, setShareFilter] = useState(() => readInitialSearchParam("share"));
  const [pathPrefix, setPathPrefix] = useState(() => readInitialSearchParam("path"));
  const [extFilter, setExtFilter] = useState(() => readInitialSearchParam("ext"));
  const [resourceAccess, setResourceAccess] = useState(() => readInitialSearchParam("access"));
  const [inventoryQueryInput, setInventoryQueryInput] = useState(initialDsl.current);
  const [appliedInventoryQuery, setAppliedInventoryQuery] = useState(initialDsl.current);
  const [appliedInventoryQueryGroups, setAppliedInventoryQueryGroups] = useState<InventoryQueryGroup[]>(() => {
    if (!initialDsl.current) return [];
    try {
      return parseInventoryQuery(initialDsl.current);
    } catch {
      return [];
    }
  });

  const [extensions, setExtensions] = useState<ExtensionFacet[]>([]);
  const [inventoryStats, setInventoryStats] = useState<InventoryStats | null>(null);
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [resources, setResources] = useState<InventoryResource[]>([]);
  const [endpoints, setEndpoints] = useState<InventoryEndpoint[]>([]);

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [inventoryError, setInventoryError] = useState<string | null>(null);
  const [inventoryLoading, setInventoryLoading] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [savedInvestigations, setSavedInvestigations] = useState<SavedInvestigation[]>([]);
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<string | null>(null);
  const [investigationName, setInvestigationName] = useState("");
  const [investigationDescription, setInvestigationDescription] = useState("");
  const [savingInvestigation, setSavingInvestigation] = useState(false);
  const [deletingInvestigationId, setDeletingInvestigationId] = useState<string | null>(null);
  const [itemColumns, setItemColumns] = useState<ItemColumnKey[]>(() =>
    readStoredColumns("share_sentinel_inventory_item_columns", ITEM_COLUMN_OPTIONS, DEFAULT_ITEM_COLUMNS),
  );
  const [resourceColumns, setResourceColumns] = useState<ResourceColumnKey[]>(() =>
    readStoredColumns("share_sentinel_inventory_resource_columns", RESOURCE_COLUMN_OPTIONS, DEFAULT_RESOURCE_COLUMNS),
  );
  const [endpointColumns, setEndpointColumns] = useState<EndpointColumnKey[]>(() =>
    readStoredColumns("share_sentinel_inventory_endpoint_columns", ENDPOINT_COLUMN_OPTIONS, DEFAULT_ENDPOINT_COLUMNS),
  );
  const [showAdvancedQuery, setShowAdvancedQuery] = useState(false);
  const [showGuidedFilters, setShowGuidedFilters] = useState(() =>
    [readInitialSearchParam("endpoint"), readInitialSearchParam("share"), readInitialSearchParam("path"), readInitialSearchParam("ext"), readInitialSearchParam("access")].some(Boolean),
  );
  const [showViewsDialog, setShowViewsDialog] = useState(false);
  const [density, setDensity] = useState<Density>(() =>
    typeof window !== "undefined" && localStorage.getItem("share_sentinel_inventory_density") === "comfortable" ? "comfortable" : "compact",
  );
  const [copiedNotice, setCopiedNotice] = useState<string | null>(null);

  const runIdsParam = useMemo(() => selectedRunIds.join(","), [selectedRunIds]);
  const debouncedQuery = useDebouncedValue(query, 300);
  const debouncedEndpointFilter = useDebouncedValue(endpointFilter, 300);
  const debouncedShareFilter = useDebouncedValue(shareFilter, 300);
  const debouncedPathPrefix = useDebouncedValue(pathPrefix, 300);
  const debouncedExtFilter = useDebouncedValue(extFilter, 300);
  const debouncedResourceAccess = useDebouncedValue(resourceAccess, 150);
  const queryModeActive = appliedInventoryQuery.trim().length > 0;
  const canImport = projectRole === "operator" || projectRole === "admin";
  const queryFilterReflections = useMemo(() => {
    if (!queryModeActive) return blankQueryFilterReflections();
    const reflections = blankQueryFilterReflections();
    for (const field of QUERYABLE_FIELDS) {
      reflections[field] = buildQueryFilterReflection(appliedInventoryQueryGroups, field);
    }
    return reflections;
  }, [appliedInventoryQueryGroups, queryModeActive]);
  const activeResultCount = activeTab === "items" ? items.length : activeTab === "resources" ? resources.length : endpoints.length;
  const activeColumnCount = activeTab === "items" ? itemColumns.length : activeTab === "resources" ? resourceColumns.length : endpointColumns.length;
  const activeRunCount = selectedRunIds.length;
  const hasGuidedFilters = [query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess].some((value) => value.trim());
  const hasActiveFilters = queryModeActive || hasGuidedFilters || selectedRunIds.length > 0;
  const eligibleRuns = useMemo(() => runs.filter((run) => run.status === "COMPLETE" || run.status === "INGESTING"), [runs]);
  const partialRunCount =
    selectedRunIds.length > 0
      ? eligibleRuns.filter((run) => selectedRunIds.includes(run.id) && run.status === "INGESTING").length
      : inventoryStats?.runs_ingesting || 0;
  const unavailableRunCount = runs.length - eligibleRuns.length;

  function clearAppliedInventoryQuery() {
    setAppliedInventoryQuery("");
    setAppliedInventoryQueryGroups([]);
    setQueryError(null);
  }

  function applyParsedInventoryQuery(groups: InventoryQueryGroup[], raw: string) {
    const reflections = blankQueryFilterReflections();
    for (const field of QUERYABLE_FIELDS) {
      reflections[field] = buildQueryFilterReflection(groups, field);
    }

    setAppliedInventoryQuery(raw.trim());
    setAppliedInventoryQueryGroups(groups);
    setQuery(reflections.search.value);
    setEndpointFilter(reflections.endpoint.value);
    setShareFilter(reflections.share.value);
    setPathPrefix(reflections.path.value);
    setExtFilter(reflections.ext.selectValue);
    setResourceAccess(reflections.access.selectValue);
    setQueryError(null);
  }

  function handleInventoryQueryApply() {
    if (!inventoryQueryInput.trim()) {
      clearAppliedInventoryQuery();
      clearSimpleFilters();
      return;
    }
    try {
      const groups = parseInventoryQuery(inventoryQueryInput);
      applyParsedInventoryQuery(groups, inventoryQueryInput);
    } catch (err) {
      setQueryError(err instanceof Error ? err.message : "Invalid inventory query.");
    }
  }

  function handleSimpleFilterChange<T>(setter: (value: T) => void, value: T) {
    if (queryModeActive) {
      clearAppliedInventoryQuery();
      clearSimpleFilters();
      setInventoryQueryInput("");
    }
    setter(value);
  }

  function clearSimpleFilters() {
    setQuery("");
    setEndpointFilter("");
    setShareFilter("");
    setPathPrefix("");
    setExtFilter("");
    setResourceAccess("");
  }

  function clearFieldFilters() {
    clearSimpleFilters();
    setInventoryQueryInput("");
    clearAppliedInventoryQuery();
  }

  function clearAllFilters() {
    clearFieldFilters();
    setSelectedRunIds([]);
  }

  function guidedFilterGroups(): InventoryQueryGroup[] {
    const group: InventoryQueryGroup = [];
    if (query.trim()) group.push({ field: "search", operator: "contains", value: query.trim(), negated: false });
    if (endpointFilter.trim()) group.push({ field: "endpoint", operator: "contains", value: endpointFilter.trim(), negated: false });
    if (shareFilter.trim()) group.push({ field: "share", operator: "contains", value: shareFilter.trim(), negated: false });
    if (pathPrefix.trim()) group.push({ field: "path", operator: "startswith", value: pathPrefix.trim(), negated: false });
    if (extFilter.trim()) group.push({ field: "ext", operator: "equals", value: extFilter.trim(), negated: false });
    if (resourceAccess.trim()) group.push({ field: "access", operator: "equals", value: resourceAccess.trim(), negated: false });
    return group.length > 0 ? [group] : [];
  }

  function applyCellFilter(field: InventoryQueryField, value: string, negated: boolean) {
    const baseGroups = queryModeActive ? appliedInventoryQueryGroups : guidedFilterGroups();
    const clause: InventoryQueryClause = { field, operator: "equals", value, negated };
    const nextGroups = baseGroups.length > 0 ? baseGroups.map((group) => [...group, clause]) : [[clause]];
    const serialized = serializeInventoryGroups(nextGroups);
    if (!serialized) {
      setQueryError("This value contains both quote styles and cannot be represented safely in the inventory query.");
      return;
    }
    setInventoryQueryInput(serialized);
    applyParsedInventoryQuery(nextGroups, serialized);
  }

  function removeAppliedQueryField(field: InventoryQueryField) {
    const nextGroups = appliedInventoryQueryGroups.map((group) => group.filter((clause) => clause.field !== field)).filter((group) => group.length > 0);
    const serialized = serializeInventoryGroups(nextGroups);
    if (serialized === null) {
      setQueryError("The remaining query could not be represented safely.");
      return;
    }
    if (!serialized) {
      setInventoryQueryInput("");
      clearAppliedInventoryQuery();
      clearFilterField(field);
      return;
    }
    setInventoryQueryInput(serialized);
    applyParsedInventoryQuery(nextGroups, serialized);
  }

  function clearFilterField(field: InventoryQueryField) {
    if (field === "search") setQuery("");
    if (field === "endpoint") setEndpointFilter("");
    if (field === "share") setShareFilter("");
    if (field === "path") setPathPrefix("");
    if (field === "ext") setExtFilter("");
    if (field === "access") setResourceAccess("");
  }

  async function copyExactValue(value: string, label: string) {
    try {
      await copyText(value);
      setCopiedNotice(`${label} copied`);
      if (copiedNoticeTimer.current) window.clearTimeout(copiedNoticeTimer.current);
      copiedNoticeTimer.current = window.setTimeout(() => setCopiedNotice(null), 1800);
    } catch {
      setCopiedNotice("Copy failed. Select the value and copy it manually.");
    }
  }

  function currentInvestigationSummary(): string {
    if (appliedInventoryQuery.trim()) return appliedInventoryQuery.trim();
    return [
      query.trim() ? `search:${query.trim()}` : null,
      endpointFilter.trim() ? `endpoint:${endpointFilter.trim()}` : null,
      shareFilter.trim() ? `share:${shareFilter.trim()}` : null,
      pathPrefix.trim() ? `path:${pathPrefix.trim()}` : null,
      extFilter.trim() ? `ext:${extFilter.trim()}` : null,
      resourceAccess.trim() ? `access:${resourceAccess.trim()}` : null,
    ]
      .filter(Boolean)
      .join(" ");
  }

  function currentInvestigationDefinition(): SavedInvestigationDefinition {
    return {
      active_tab: activeTab,
      selected_run_ids: selectedRunIds,
      filters: {
        query,
        endpoint_filter: endpointFilter,
        share_filter: shareFilter,
        path_prefix: pathPrefix,
        ext_filter: extFilter,
        resource_access: resourceAccess,
      },
      applied_query: appliedInventoryQuery.trim(),
      draft_query: inventoryQueryInput.trim(),
    };
  }

  async function refreshSavedInvestigations(
    nextSelectedId?: string | null,
    signal?: AbortSignal,
    expectedProjectId: string | undefined = projectId,
  ) {
    if (!expectedProjectId) return;
    const data = await apiFetch(`/projects/${expectedProjectId}/inventory/investigations`, { signal });
    if (signal?.aborted || projectIdRef.current !== expectedProjectId) return;
    const items = ((data?.items || []) as SavedInvestigation[]).map((item) => ({
      ...item,
      target_tab: isTab(item.target_tab) ? item.target_tab : "items",
      definition: typeof item.definition === "object" && item.definition ? item.definition : {},
    }));
    setSavedInvestigations(items);
    if (nextSelectedId !== undefined) {
      setSelectedInvestigationId(items.some((item) => item.id === nextSelectedId) ? nextSelectedId : null);
      return;
    }
    setSelectedInvestigationId((current) => (current && items.some((item) => item.id === current) ? current : null));
  }

  function applySavedInvestigation(investigation: SavedInvestigation) {
    const definition = investigation.definition || {};
    const targetTab: Tab = isTab(definition.active_tab || "") ? definition.active_tab || investigation.target_tab : investigation.target_tab;
    const filters = typeof definition.filters === "object" && definition.filters ? definition.filters : {};
    const appliedQuery = typeof definition.applied_query === "string" ? definition.applied_query.trim() : "";
    const draftQuery =
      typeof definition.draft_query === "string" && definition.draft_query.trim().length > 0
        ? definition.draft_query
        : appliedQuery;
    const selectedRuns = Array.isArray(definition.selected_run_ids)
      ? definition.selected_run_ids.filter((value): value is string => typeof value === "string")
      : [];

    setActiveTab(targetTab);
    setSelectedRunIds(selectedRuns);
    setQuery(typeof filters.query === "string" ? filters.query : "");
    setEndpointFilter(typeof filters.endpoint_filter === "string" ? filters.endpoint_filter : "");
    setShareFilter(typeof filters.share_filter === "string" ? filters.share_filter : "");
    setPathPrefix(typeof filters.path_prefix === "string" ? filters.path_prefix : "");
    setExtFilter(typeof filters.ext_filter === "string" ? filters.ext_filter : "");
    setResourceAccess(typeof filters.resource_access === "string" ? filters.resource_access : "");
    setInventoryQueryInput(draftQuery);
    setSelectedInvestigationId(investigation.id);
    setInvestigationName(investigation.name);
    setInvestigationDescription(investigation.description || "");
    setError(null);

    if (!appliedQuery) {
      clearAppliedInventoryQuery();
      return;
    }
    try {
      const groups = parseInventoryQuery(appliedQuery);
      applyParsedInventoryQuery(groups, appliedQuery);
    } catch (err) {
      clearAppliedInventoryQuery();
      setQueryError(err instanceof Error ? err.message : "Saved investigation query is invalid.");
    }
  }

  async function saveInvestigation() {
    if (!projectId) return;
    const targetProjectId = projectId;
    const name = investigationName.trim();
    if (!name) {
      setError("Name the shared investigation before saving it.");
      return;
    }

    setSavingInvestigation(true);
    setError(null);
    try {
      const created = (await apiFetch(`/projects/${targetProjectId}/inventory/investigations`, {
        method: "POST",
        body: JSON.stringify({
          name,
          description: investigationDescription.trim() || null,
          target_tab: activeTab,
          query_text: currentInvestigationSummary(),
          definition: currentInvestigationDefinition(),
        }),
      })) as SavedInvestigation;
      if (projectIdRef.current !== targetProjectId) return;
      await refreshSavedInvestigations(created.id, undefined, targetProjectId);
      if (projectIdRef.current !== targetProjectId) return;
      setSelectedInvestigationId(created.id);
    } catch (err) {
      if (projectIdRef.current === targetProjectId) {
        setError(err instanceof Error ? err.message : "Failed to save investigation.");
      }
    } finally {
      if (projectIdRef.current === targetProjectId) setSavingInvestigation(false);
    }
  }

  async function updateInvestigation() {
    if (!projectId || !selectedInvestigationId) return;
    const targetProjectId = projectId;
    const targetInvestigationId = selectedInvestigationId;
    const name = investigationName.trim();
    if (!name) {
      setError("Name the shared investigation before updating it.");
      return;
    }

    setSavingInvestigation(true);
    setError(null);
    try {
      await apiFetch(`/projects/${targetProjectId}/inventory/investigations/${targetInvestigationId}`, {
        method: "PATCH",
        body: JSON.stringify({
          name,
          description: investigationDescription.trim() || null,
          target_tab: activeTab,
          query_text: currentInvestigationSummary(),
          definition: currentInvestigationDefinition(),
        }),
      });
      if (projectIdRef.current !== targetProjectId) return;
      await refreshSavedInvestigations(targetInvestigationId, undefined, targetProjectId);
    } catch (err) {
      if (projectIdRef.current === targetProjectId) {
        setError(err instanceof Error ? err.message : "Failed to update investigation.");
      }
    } finally {
      if (projectIdRef.current === targetProjectId) setSavingInvestigation(false);
    }
  }

  async function deleteInvestigation(investigation: SavedInvestigation) {
    if (!projectId) return;
    const targetProjectId = projectId;
    if (!window.confirm(`Delete shared investigation "${investigation.name}"?`)) {
      return;
    }

    setDeletingInvestigationId(investigation.id);
    setError(null);
    try {
      await apiFetch(`/projects/${targetProjectId}/inventory/investigations/${investigation.id}`, {
        method: "DELETE",
      });
      if (projectIdRef.current !== targetProjectId) return;
      await refreshSavedInvestigations(
        investigation.id === selectedInvestigationId ? null : undefined,
        undefined,
        targetProjectId,
      );
      if (projectIdRef.current !== targetProjectId) return;
      if (investigation.id === selectedInvestigationId) {
        setInvestigationName("");
        setInvestigationDescription("");
      }
    } catch (err) {
      if (projectIdRef.current === targetProjectId) {
        setError(err instanceof Error ? err.message : "Failed to delete investigation.");
      }
    } finally {
      if (projectIdRef.current === targetProjectId) setDeletingInvestigationId(null);
    }
  }

  useEffect(() => {
    const next = new URLSearchParams();
    if (activeTab !== "items") next.set("tab", activeTab);
    if (selectedRunIds.length > 0) next.set("runs", selectedRunIds.join(","));
    if (queryModeActive) {
      next.set("queryDsl", appliedInventoryQuery.trim());
    } else {
      if (query.trim()) next.set("q", query.trim());
      if (endpointFilter.trim()) next.set("endpoint", endpointFilter.trim());
      if (shareFilter.trim()) next.set("share", shareFilter.trim());
      if (pathPrefix.trim()) next.set("path", pathPrefix.trim());
      if (extFilter.trim()) next.set("ext", extFilter.trim());
      if (resourceAccess.trim()) next.set("access", resourceAccess.trim());
    }
    setSearchParams(next, { replace: true });
  }, [activeTab, appliedInventoryQuery, endpointFilter, extFilter, pathPrefix, query, queryModeActive, resourceAccess, selectedRunIds, setSearchParams, shareFilter]);

  useEffect(() => {
    localStorage.setItem("share_sentinel_inventory_item_columns", JSON.stringify(itemColumns));
  }, [itemColumns]);

  useEffect(() => {
    localStorage.setItem("share_sentinel_inventory_resource_columns", JSON.stringify(resourceColumns));
  }, [resourceColumns]);

  useEffect(() => {
    localStorage.setItem("share_sentinel_inventory_endpoint_columns", JSON.stringify(endpointColumns));
  }, [endpointColumns]);

  useEffect(() => {
    localStorage.setItem("share_sentinel_inventory_density", density);
  }, [density]);

  useEffect(() => {
    const handleKeyboardShortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = !!target?.closest("input, textarea, select, [contenteditable='true']");
      if (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyboardShortcut);
    return () => window.removeEventListener("keydown", handleKeyboardShortcut);
  }, []);

  useEffect(
    () => () => {
      if (copiedNoticeTimer.current) window.clearTimeout(copiedNoticeTimer.current);
    },
    [],
  );

  useEffect(() => {
    if (!initialDsl.current) return;
    try {
      parseInventoryQuery(initialDsl.current);
    } catch (err) {
      setQueryError(err instanceof Error ? err.message : "The inventory query in this URL is invalid.");
      setAppliedInventoryQuery("");
      setAppliedInventoryQueryGroups([]);
    }
  }, []);

  useEffect(() => {
    if (queryModeActive || queryError) {
      setShowAdvancedQuery(true);
    }
  }, [queryError, queryModeActive]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    const controller = new AbortController();
    setProject(null);
    setRuns([]);
    setRunsLoaded(false);
    setSelectedRunIds(readInitialRuns());
    setExtensions([]);
    setInventoryStats(null);
    setSavedInvestigations([]);
    setSelectedInvestigationId(null);
    setInvestigationName("");
    setInvestigationDescription("");
    setSavingInvestigation(false);
    setDeletingInvestigationId(null);
    setShowViewsDialog(false);
    setItems([]);
    setResources([]);
    setEndpoints([]);
    setCursor(null);
    setCursorHistory([]);
    setNextCursor(null);
    setLastLoadedAt(null);
    setError(null);
    setInventoryError(null);
    setProjectRoleStatus("loading");
    apiFetch(`/projects/${projectId}/my-role`, { signal: controller.signal })
      .then((data) => {
        if (cancelled) return;
        setProjectRole((data?.role as string) || null);
        setProjectRoleStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setProjectRole(null);
        setProjectRoleStatus("error");
      });

    apiFetch(`/projects/${projectId}`, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) setProject(data as Project);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });

    apiFetchAllPages<RunOption>((cursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (cursor) query.set("cursor", cursor);
      return `/projects/${projectId}/runs?${query.toString()}`;
    }, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) {
          setRuns(data);
          setRunsLoaded(true);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });

    apiFetch(`/projects/${projectId}/inventory/stats`, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) setInventoryStats((data || null) as InventoryStats | null);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load inventory coverage status.");
      });

    refreshSavedInvestigations(undefined, controller.signal).catch((err) => {
      if (!cancelled && !isAbortError(err)) setError(err instanceof Error ? err.message : "Failed to load investigations.");
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [projectId]);

  useEffect(() => {
    if (!runsLoaded) return;
    const eligibleIds = new Set(eligibleRuns.map((run) => run.id));
    setSelectedRunIds((current) => current.filter((id) => eligibleIds.has(id)));
  }, [eligibleRuns, runsLoaded]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    const queryParams = new URLSearchParams({ limit: "100" });
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);

    apiFetch(`/projects/${projectId}/inventory/extensions?${queryParams.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setExtensions((data?.items || []) as ExtensionFacet[]);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) setError(err.message);
      });
    return () => controller.abort();
  }, [projectId, runIdsParam]);

  useEffect(() => {
    setCursor(null);
    setCursorHistory([]);
  }, [activeTab, appliedInventoryQuery, projectId, runIdsParam, debouncedQuery, debouncedEndpointFilter, debouncedShareFilter, debouncedPathPrefix, debouncedExtFilter, debouncedResourceAccess]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    const queryParams = new URLSearchParams({ limit: "200" });
    if (cursor) queryParams.set("cursor", cursor);
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);
    if (queryModeActive) queryParams.set("query_dsl", appliedInventoryQuery.trim());
    if (!queryModeActive) {
      const guidedGroup: InventoryQueryGroup = [];
      if (debouncedQuery.trim()) guidedGroup.push({ field: "search", operator: "contains", value: debouncedQuery.trim(), negated: false });
      if (debouncedEndpointFilter.trim()) guidedGroup.push({ field: "endpoint", operator: "contains", value: debouncedEndpointFilter.trim(), negated: false });
      if (debouncedShareFilter.trim()) guidedGroup.push({ field: "share", operator: "contains", value: debouncedShareFilter.trim(), negated: false });
      if (debouncedPathPrefix.trim()) guidedGroup.push({ field: "path", operator: "startswith", value: debouncedPathPrefix.trim(), negated: false });
      if (debouncedExtFilter.trim()) guidedGroup.push({ field: "ext", operator: "equals", value: debouncedExtFilter.trim(), negated: false });
      if (debouncedResourceAccess.trim()) guidedGroup.push({ field: "access", operator: "equals", value: debouncedResourceAccess.trim(), negated: false });
      const serialized = serializeInventoryGroups(guidedGroup.length > 0 ? [guidedGroup] : []);
      if (serialized) queryParams.set("query_dsl", serialized);
    }

    let path: string;
    if (activeTab === "items") {
      path = `/projects/${projectId}/inventory/items?${queryParams.toString()}`;
    } else if (activeTab === "resources") {
      path = `/projects/${projectId}/inventory/resources?${queryParams.toString()}`;
    } else {
      path = `/projects/${projectId}/inventory/endpoints?${queryParams.toString()}`;
    }

    setInventoryLoading(true);
    setInventoryError(null);
    setNextCursor(null);
    if (activeTab === "items") setItems([]);
    if (activeTab === "resources") setResources([]);
    if (activeTab === "endpoints") setEndpoints([]);
    apiFetch(path, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        if (activeTab === "items") setItems((data?.items || []) as InventoryItem[]);
        if (activeTab === "resources") setResources((data?.items || []) as InventoryResource[]);
        if (activeTab === "endpoints") setEndpoints((data?.items || []) as InventoryEndpoint[]);
        setNextCursor((data?.next_cursor as string | null) || null);
        setLastLoadedAt(new Date());
      })
      .catch((err) => {
        if (!controller.signal.aborted && !isAbortError(err)) {
          setInventoryError(err instanceof Error ? err.message : "Inventory request failed.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setInventoryLoading(false);
      });

    return () => controller.abort();
  }, [
    activeTab,
    appliedInventoryQuery,
    cursor,
    debouncedEndpointFilter,
    debouncedExtFilter,
    debouncedPathPrefix,
    debouncedQuery,
    debouncedResourceAccess,
    debouncedShareFilter,
    projectId,
    queryModeActive,
    refreshNonce,
    runIdsParam,
  ]);

  function moveNext() {
    if (!nextCursor) return;
    setCursorHistory((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }

  function movePrev() {
    setCursorHistory((prev) => {
      if (prev.length === 0) return prev;
      const copy = [...prev];
      const previous = copy.pop() ?? null;
      setCursor(previous);
      return copy;
    });
  }

  function toggleItemColumn(column: ItemColumnKey) {
    setItemColumns((prev) => {
      if (prev.includes(column)) {
        if (prev.length === 1) return prev;
        return prev.filter((key) => key !== column);
      }
      return [...prev, column];
    });
  }

  function moveColumn<T>(rows: T[], column: T, direction: -1 | 1): T[] {
    const sourceIndex = rows.indexOf(column);
    const targetIndex = sourceIndex + direction;
    if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= rows.length) return rows;
    const next = [...rows];
    [next[sourceIndex], next[targetIndex]] = [next[targetIndex], next[sourceIndex]];
    return next;
  }

  function moveItemColumn(column: ItemColumnKey, direction: -1 | 1) {
    setItemColumns((prev) => moveColumn(prev, column, direction));
  }

  function moveResourceColumn(column: ResourceColumnKey, direction: -1 | 1) {
    setResourceColumns((prev) => moveColumn(prev, column, direction));
  }

  function moveEndpointColumn(column: EndpointColumnKey, direction: -1 | 1) {
    setEndpointColumns((prev) => moveColumn(prev, column, direction));
  }

  function toggleResourceColumn(column: ResourceColumnKey) {
    setResourceColumns((prev) => {
      if (prev.includes(column)) {
        if (prev.length === 1) return prev;
        return prev.filter((key) => key !== column);
      }
      return [...prev, column];
    });
  }

  function toggleEndpointColumn(column: EndpointColumnKey) {
    setEndpointColumns((prev) => {
      if (prev.includes(column)) {
        if (prev.length === 1) return prev;
        return prev.filter((key) => key !== column);
      }
      return [...prev, column];
    });
  }

  function itemCell(row: InventoryItem, column: ItemColumnKey): ReactNode {
    const label = ITEM_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    if (column === "path") return <InventoryCell text={row.path} label={label} filterField="path" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "name") return <InventoryCell text={row.name} label={label} filterField="search" filterScopeLabel="any searchable item field" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "resource_name") return <InventoryCell text={row.resource_name} label={label} filterField="share" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "share_type") return <InventoryCell text={row.share_type.toUpperCase()} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "access_level") {
      return (
        <AccessCapabilityCell
          accessLevel={row.access_level}
          capabilities={row.access_capabilities}
          evidenceScope="Share sample"
          label={label}
          onCopy={copyExactValue}
          onFilter={(value, negated) => applyCellFilter("access", value, negated)}
        />
      );
    }
    if (column === "endpoint_key") return <InventoryCell text={row.endpoint_key} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "hostname") return <InventoryCell text={row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "ip") return <InventoryCell text={row.ip || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_name") return <InventoryCell text={row.run_name} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_id") return <InventoryCell text={row.run_id} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "size_bytes") return <InventoryCell text={formatBytes(row.size_bytes)} filterValue={row.size_bytes == null ? "" : String(row.size_bytes)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "allocation_size_bytes") return <InventoryCell text={formatBytes(row.allocation_size_bytes)} filterValue={row.allocation_size_bytes == null ? "" : String(row.allocation_size_bytes)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "mtime") return <InventoryCell text={formatTimestamp(row.mtime)} filterValue={row.mtime || ""} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "created_at") return <InventoryCell text={formatTimestamp(row.created_at)} filterValue={row.created_at || ""} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "accessed_at") return <InventoryCell text={formatTimestamp(row.accessed_at)} filterValue={row.accessed_at || ""} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "changed_at") return <InventoryCell text={formatTimestamp(row.changed_at)} filterValue={row.changed_at || ""} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "file_attributes") return <InventoryCell text={formatFileAttributes(row.file_attributes)} filterValue={row.file_attributes?.join(",") || ""} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.is_dir ? "Directory" : "File"} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  function resourceCell(row: InventoryResource, column: ResourceColumnKey): ReactNode {
    const label = RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    if (column === "name") return <InventoryCell text={row.name} label={label} filterField="share" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "share_type") return <InventoryCell text={row.share_type.toUpperCase()} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "access_level") {
      return (
        <AccessCapabilityCell
          accessLevel={row.access_level}
          capabilities={row.access_capabilities}
          label={label}
          onCopy={copyExactValue}
          onFilter={(value, negated) => applyCellFilter("access", value, negated)}
        />
      );
    }
    if (column === "endpoint_key") return <InventoryCell text={row.endpoint_key} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "hostname") return <InventoryCell text={row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "item_count") return <InventoryCell text={row.item_count.toLocaleString()} filterValue={String(row.item_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_name") return <InventoryCell text={row.run_name} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_id") return <InventoryCell text={row.run_id} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.remark || "—"} label={label} filterField="search" filterScopeLabel="any searchable share field" onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  function endpointCell(row: InventoryEndpoint, column: EndpointColumnKey): ReactNode {
    const label = ENDPOINT_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    if (column === "endpoint_key") return <InventoryCell text={row.endpoint_key} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "hostname") return <InventoryCell text={row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "ip") return <InventoryCell text={row.ip || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "domain") return <InventoryCell text={row.domain || "—"} label={label} filterField="search" filterScopeLabel="any searchable endpoint field" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "smb_signing") return <InventoryCell text={row.smb_signing || "—"} label={label} badge={row.smb_signing?.toLowerCase() === "required" ? "positive" : "neutral"} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "resource_count") return <InventoryCell text={row.resource_count.toLocaleString()} filterValue={String(row.resource_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "item_count") return <InventoryCell text={row.item_count.toLocaleString()} filterValue={String(row.item_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_name") return <InventoryCell text={row.run_name} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.run_id} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  const activeTabCopy = INVENTORY_TAB_COPY[activeTab];
  const guidedFilterCount = queryModeActive
    ? QUERYABLE_FIELDS.filter((field) => !!queryFilterReflections[field].summary).length
    : [query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess].filter((value) => value.trim()).length;
  const currentPage = cursorHistory.length + 1;

  return (
    <section className="inventory-workspace">
      <header className="inventory-page-header">
        <div className="inventory-heading-copy">
          <nav aria-label="Breadcrumb" className="inventory-breadcrumb">
            <Link to="/projects">Projects</Link>
            <span aria-hidden="true">/</span>
            <span>{project?.name || projectId || "Project"}</span>
            <span aria-hidden="true">/</span>
            <span>Inventory</span>
          </nav>
          <h1>{project?.name || "Project inventory"}</h1>
          <p>Browse collected files, shares, and endpoints without losing investigation context.</p>
        </div>
        <div className="inventory-header-actions">
          {projectId && canImport ? (
            <Link className="inventory-button-primary" to={`/projects/${projectId}/import`}>
              Import scan
            </Link>
          ) : null}
          {projectId && projectRoleStatus === "loading" ? <span className="inventory-muted-status">Checking import access…</span> : null}
        </div>
      </header>

      {error ? (
        <StatusBanner tone="error" title="Some project context could not be loaded">
          <p>{error}</p>
        </StatusBanner>
      ) : null}
      {partialRunCount > 0 ? (
        <StatusBanner tone="warning" title="Inventory is still changing">
          <p>
            {partialRunCount} run{partialRunCount === 1 ? " is" : "s are"} still ingesting. Results include committed records and may be incomplete until ingestion finishes.
          </p>
        </StatusBanner>
      ) : null}

      <div className="inventory-console">
        <div className="inventory-console-topline">
          <div aria-label="Inventory object type" className="inventory-tabs">
            {(["items", "resources", "endpoints"] as Tab[]).map((tab) => (
              <button
                aria-pressed={activeTab === tab}
                className={activeTab === tab ? "is-active" : ""}
                key={tab}
                onClick={() => setActiveTab(tab)}
                type="button"
              >
                {INVENTORY_TAB_COPY[tab].label}
              </button>
            ))}
          </div>
          <div className="inventory-result-meta" aria-live="polite">
            <strong>{inventoryLoading && !lastLoadedAt ? "Loading" : `${activeResultCount.toLocaleString()} shown`}</strong>
            <span>{nextCursor ? "More available" : `Page ${currentPage}`}</span>
            {lastLoadedAt ? <span title={lastLoadedAt.toLocaleString()}>Updated {lastLoadedAt.toLocaleTimeString()}</span> : null}
          </div>
        </div>

        <div className="inventory-commandbar">
          <label className="inventory-search">
            <span className="sr-only">Search {activeTabCopy.label.toLowerCase()}</span>
            <span aria-hidden="true" className="inventory-search-icon">⌕</span>
            <input
              autoComplete="off"
              onChange={(event) => handleSimpleFilterChange(setQuery, event.target.value)}
              placeholder={activeTab === "items" ? "Search file, path, share, or endpoint" : activeTab === "resources" ? "Search share, remark, or endpoint" : "Search hostname, address, domain, or key"}
              ref={searchInputRef}
              type="search"
              value={query}
            />
            {query ? (
              <button aria-label="Clear search" onClick={() => handleSimpleFilterChange(setQuery, "")} type="button">
                ×
              </button>
            ) : (
              <kbd>/</kbd>
            )}
          </label>

          <button
            aria-expanded={showGuidedFilters}
            className={`inventory-toolbar-button ${showGuidedFilters ? "is-active" : ""}`}
            onClick={() => setShowGuidedFilters((current) => !current)}
            type="button"
          >
            Filters {guidedFilterCount > 0 ? <span className="inventory-toolbar-count">{guidedFilterCount}</span> : null}
          </button>

          <details className="inventory-popover">
            <summary className="inventory-toolbar-button">
              Runs {activeRunCount > 0 ? <span className="inventory-toolbar-count">{activeRunCount}</span> : null}
            </summary>
            <div className="inventory-popover-panel inventory-run-panel">
              <div className="inventory-popover-heading">
                <div>
                  <p className="inventory-popover-title">Run scope</p>
                  <p className="inventory-popover-copy">No selection includes every eligible run.</p>
                </div>
                <button className="inventory-text-button" disabled={activeRunCount === 0} onClick={() => setSelectedRunIds([])} type="button">
                  Clear
                </button>
              </div>
              <div className="inventory-run-list">
                {eligibleRuns.length === 0 ? <p className="inventory-empty-copy">No completed or ingesting runs are available.</p> : null}
                {unavailableRunCount > 0 ? <p className="inventory-run-note">{unavailableRunCount} pending, uploaded, or failed run{unavailableRunCount === 1 ? " is" : "s are"} excluded because they have no queryable inventory.</p> : null}
                {eligibleRuns.map((run) => (
                  <label className="inventory-run-option" key={run.id}>
                    <input
                      checked={selectedRunIds.includes(run.id)}
                      onChange={(event) =>
                        setSelectedRunIds((current) =>
                          event.target.checked ? [...current, run.id] : current.filter((id) => id !== run.id),
                        )
                      }
                      type="checkbox"
                    />
                    <span>
                      <strong>{run.name}</strong>
                      <small>{run.status.replaceAll("_", " ")} · {new Date(run.created_at).toLocaleString()}</small>
                    </span>
                  </label>
                ))}
              </div>
            </div>
          </details>

          <button className="inventory-toolbar-button" onClick={() => setShowViewsDialog(true)} type="button">
            Saved views {savedInvestigations.length > 0 ? <span className="inventory-toolbar-count">{savedInvestigations.length}</span> : null}
          </button>

          {activeTab === "items" ? (
            <ColumnPicker options={ITEM_COLUMN_OPTIONS} selected={itemColumns} onToggle={toggleItemColumn} onMove={moveItemColumn} onReset={() => setItemColumns(DEFAULT_ITEM_COLUMNS)} />
          ) : null}
          {activeTab === "resources" ? (
            <ColumnPicker options={RESOURCE_COLUMN_OPTIONS} selected={resourceColumns} onToggle={toggleResourceColumn} onMove={moveResourceColumn} onReset={() => setResourceColumns(DEFAULT_RESOURCE_COLUMNS)} />
          ) : null}
          {activeTab === "endpoints" ? (
            <ColumnPicker options={ENDPOINT_COLUMN_OPTIONS} selected={endpointColumns} onToggle={toggleEndpointColumn} onMove={moveEndpointColumn} onReset={() => setEndpointColumns(DEFAULT_ENDPOINT_COLUMNS)} />
          ) : null}

          <button
            aria-label={`Use ${density === "compact" ? "comfortable" : "compact"} table density`}
            className="inventory-toolbar-button"
            onClick={() => setDensity((current) => (current === "compact" ? "comfortable" : "compact"))}
            title="Change row density"
            type="button"
          >
            {density === "compact" ? "Compact" : "Comfortable"}
          </button>
          <button
            aria-label="Refresh inventory"
            className="inventory-toolbar-button is-icon"
            disabled={inventoryLoading}
            onClick={() => setRefreshNonce((current) => current + 1)}
            title="Refresh inventory"
            type="button"
          >
            ↻
          </button>
        </div>

        {hasActiveFilters ? (
          <div className="inventory-active-filters" aria-label="Active filters">
            <span className="inventory-active-label">Active</span>
            {queryModeActive
              ? QUERYABLE_FIELDS.map((field) =>
                  queryFilterReflections[field].summary ? (
                    <span className="inventory-filter-chip" key={field}>
                      <strong>{QUERY_FIELD_LABELS[field]}</strong> {queryFilterReflections[field].summary}
                      <button aria-label={`Remove ${QUERY_FIELD_LABELS[field]} filter`} onClick={() => removeAppliedQueryField(field)} type="button">
                        ×
                      </button>
                    </span>
                  ) : null,
                )
              : (
                  [
                    ["search", "Search", query],
                    ["endpoint", "Endpoint", endpointFilter],
                    ["share", "Share", shareFilter],
                    ["path", "Path", pathPrefix],
                    ["ext", "Extension", extFilter],
                    ["access", "Access", resourceAccess],
                  ] as Array<[InventoryQueryField, string, string]>
                ).map(([field, label, value]) =>
                  value.trim() ? (
                    <span className="inventory-filter-chip" key={field}>
                      <strong>{label}</strong> {value}
                      <button aria-label={`Remove ${label} filter`} onClick={() => clearFilterField(field)} type="button">
                        ×
                      </button>
                    </span>
                  ) : null,
                )}
            {activeRunCount > 0 ? (
              <span className="inventory-filter-chip">
                <strong>Runs</strong> {activeRunCount} selected
                <button aria-label="Clear run scope" onClick={() => setSelectedRunIds([])} type="button">
                  ×
                </button>
              </span>
            ) : null}
            <button className="inventory-clear-all" onClick={clearAllFilters} type="button">
              Clear all
            </button>
          </div>
        ) : (
          <p className="inventory-command-hint">Tip: focus search with <kbd>/</kbd>. Hover a table value to filter, exclude, or copy it.</p>
        )}

        {showGuidedFilters ? (
          <section aria-label="Guided filters" className="inventory-filter-panel">
            <div className="inventory-filter-panel-heading">
              <div>
                <h2>Filters</h2>
                <p>These apply to the complete server-side result set, not only this page.</p>
              </div>
              <div className="inventory-inline-actions">
                <button onClick={() => setShowAdvancedQuery((current) => !current)} type="button">
                  {showAdvancedQuery ? "Hide query" : "Advanced query"}
                </button>
                <button disabled={!hasGuidedFilters && !queryModeActive} onClick={clearFieldFilters} type="button">
                  Clear filters
                </button>
              </div>
            </div>

            <div className="inventory-filter-grid">
              <label className={FILTER_LABEL_CLASS}>
                Endpoint
                <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)} placeholder="Hostname, key, or address" value={endpointFilter} />
              </label>
              {activeTab !== "endpoints" ? (
                <label className={FILTER_LABEL_CLASS}>
                  Share
                  <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setShareFilter, event.target.value)} placeholder="Finance" value={shareFilter} />
                </label>
              ) : null}
              {activeTab === "items" ? (
                <>
                  <label className={FILTER_LABEL_CLASS}>
                    Path starts with
                    <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setPathPrefix, event.target.value)} placeholder="\\Finance\\Quarterly" value={pathPrefix} />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Extension
                    <input className={FILTER_INPUT_CLASS} list="inventory-extension-options" onChange={(event) => handleSimpleFilterChange(setExtFilter, event.target.value)} placeholder=".pst" value={extFilter} />
                  </label>
                </>
              ) : null}
              {activeTab === "resources" ? (
                <label className={FILTER_LABEL_CLASS}>
                  Access
                  <select className={FILTER_SELECT_CLASS} onChange={(event) => handleSimpleFilterChange(setResourceAccess, event.target.value)} value={resourceAccess}>
                    <option value="">Any access</option>
                    <option value="readable">Read observed</option>
                    <option value="list_only">List observed</option>
                    <option value="no_access">Access denied</option>
                    <option value="unknown">Unknown</option>
                  </select>
                </label>
              ) : null}
            </div>

            {activeTab === "items" && extensions.length > 0 ? (
              <div className="inventory-facet-row" aria-label="Common extensions">
                <span>Common</span>
                {extensions.slice(0, 8).map((facet) => (
                  <button className={extFilter === facet.ext ? "is-active" : ""} key={facet.ext} onClick={() => handleSimpleFilterChange(setExtFilter, extFilter === facet.ext ? "" : facet.ext)} type="button">
                    {facet.ext} <small>{facet.count.toLocaleString()}</small>
                  </button>
                ))}
              </div>
            ) : null}
          </section>
        ) : null}

        {showAdvancedQuery ? (
          <section aria-label="Advanced inventory query" className="inventory-query-panel">
            <div className="inventory-query-main">
              <label htmlFor="inventory-query">Advanced query</label>
              <textarea
                aria-describedby="inventory-query-help"
                id="inventory-query"
                onChange={(event) => setInventoryQueryInput(event.target.value)}
                placeholder={'endpoint ^ "fs-" AND share ~ "finance" AND !ext = ".tmp"'}
                value={inventoryQueryInput}
              />
              <p id="inventory-query-help">Fields: search, endpoint, share, path, ext, access. Operators: = exact, ~ contains, ^ starts with, AND, OR, ! not.</p>
            </div>
            <div className="inventory-query-actions">
              <button className="inventory-button-primary" onClick={handleInventoryQueryApply} type="button">Apply query</button>
              <button className="inventory-button-secondary" onClick={() => setInventoryQueryInput('share ~ "finance" AND ext = ".xlsx"')} type="button">Example</button>
              <button className="inventory-button-secondary" onClick={() => { setInventoryQueryInput(""); clearSimpleFilters(); clearAppliedInventoryQuery(); }} type="button">Clear</button>
            </div>
            {queryError ? <p className="inventory-query-error" role="alert">{queryError}</p> : null}
          </section>
        ) : null}
      </div>

      <section aria-busy={inventoryLoading} aria-labelledby="inventory-results-title" className="inventory-results" id="inventory-results">
        <div className="inventory-results-header">
          <div>
            <h2 id="inventory-results-title">{activeTabCopy.label}</h2>
            <p>{activeTabCopy.description}</p>
          </div>
          <span>{activeColumnCount} columns · server order</span>
        </div>

        {inventoryError ? (
          <div className="inventory-inline-error" role="alert">
            <div>
              <strong>Inventory could not be loaded</strong>
              <p>{inventoryError}</p>
            </div>
            <button onClick={() => setRefreshNonce((current) => current + 1)} type="button">Retry</button>
          </div>
        ) : null}

        {inventoryLoading && activeResultCount === 0 ? (
          <div aria-label="Loading inventory" className="inventory-skeleton" role="status">
            {Array.from({ length: 8 }, (_, index) => <span key={index} />)}
          </div>
        ) : null}

        {!inventoryLoading && !inventoryError && activeResultCount === 0 ? (
          <StatePanel
            actions={hasActiveFilters ? <button className="inventory-button-secondary" onClick={clearAllFilters} type="button">Clear filters</button> : projectId && canImport ? <Link className="inventory-button-primary" to={`/projects/${projectId}/import`}>Import a scan</Link> : null}
            description={hasActiveFilters ? activeTabCopy.emptyBody : "No collected records are available in this project yet."}
            title={hasActiveFilters ? activeTabCopy.emptyTitle : "No inventory data"}
          />
        ) : null}

        {activeResultCount > 0 ? (
          <div className={`inventory-table-scroll is-${density}`}>
            {inventoryLoading ? <div className="inventory-loading-strip" role="status">Refreshing results…</div> : null}
            {activeTab === "items" ? (
              <table className="inventory-table">
                <caption className="sr-only">Files and folders in the selected inventory scope</caption>
                <thead><tr>{itemColumns.map((column) => <th key={column} scope="col"><span>{ITEM_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>{itemColumns.length > 1 ? <button aria-label={`Hide ${column} column`} onClick={() => toggleItemColumn(column)} title="Hide column" type="button">×</button> : null}</th>)}</tr></thead>
                <tbody>{items.map((row) => <tr key={`${row.run_id}-${row.id}`}>{itemColumns.map((column) => <td key={`${row.id}-${column}`}>{itemCell(row, column)}</td>)}</tr>)}</tbody>
              </table>
            ) : null}
            {activeTab === "resources" ? (
              <table className="inventory-table">
                <caption className="sr-only">Shares in the selected inventory scope</caption>
                <thead><tr>{resourceColumns.map((column) => <th key={column} scope="col"><span>{RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>{resourceColumns.length > 1 ? <button aria-label={`Hide ${column} column`} onClick={() => toggleResourceColumn(column)} title="Hide column" type="button">×</button> : null}</th>)}</tr></thead>
                <tbody>{resources.map((row) => <tr key={`${row.run_id}-${row.id}`}>{resourceColumns.map((column) => <td key={`${row.id}-${column}`}>{resourceCell(row, column)}</td>)}</tr>)}</tbody>
              </table>
            ) : null}
            {activeTab === "endpoints" ? (
              <table className="inventory-table">
                <caption className="sr-only">Endpoints in the selected inventory scope</caption>
                <thead><tr>{endpointColumns.map((column) => <th key={column} scope="col"><span>{ENDPOINT_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>{endpointColumns.length > 1 ? <button aria-label={`Hide ${column} column`} onClick={() => toggleEndpointColumn(column)} title="Hide column" type="button">×</button> : null}</th>)}</tr></thead>
                <tbody>{endpoints.map((row) => <tr key={`${row.run_id}-${row.id}`}>{endpointColumns.map((column) => <td key={`${row.id}-${column}`}>{endpointCell(row, column)}</td>)}</tr>)}</tbody>
              </table>
            ) : null}
          </div>
        ) : null}

        <footer className="inventory-pagination">
          <span>Page {currentPage} · up to 200 rows per page</span>
          <div>
            <button disabled={cursorHistory.length === 0 || inventoryLoading} onClick={movePrev} type="button">Previous</button>
            <button disabled={!nextCursor || inventoryLoading} onClick={moveNext} type="button">Next</button>
          </div>
        </footer>
      </section>

      <Dialog
        description="Open a team view or save the current tab, run scope, and filters."
        onClose={() => setShowViewsDialog(false)}
        open={showViewsDialog}
        size="lg"
        title="Saved inventory views"
      >
        <div className="inventory-saved-layout">
          <section className="inventory-saved-list" aria-label="Saved views">
            <div className="inventory-dialog-section-heading">
              <h3>Team views</h3>
              <span>{savedInvestigations.length}</span>
            </div>
            {savedInvestigations.length === 0 ? <p className="inventory-empty-copy">No saved views yet.</p> : null}
            {savedInvestigations.map((investigation) => (
              <article className={investigation.id === selectedInvestigationId ? "is-selected" : ""} key={investigation.id}>
                <div>
                  <strong>{investigation.name}</strong>
                  <small>{INVENTORY_TAB_COPY[investigation.target_tab].label} · Updated {new Date(investigation.updated_at).toLocaleString()}</small>
                  {investigation.description ? <p>{investigation.description}</p> : null}
                  {investigation.query_text ? <code>{investigation.query_text}</code> : null}
                </div>
                <div className="inventory-inline-actions">
                  <button onClick={() => { applySavedInvestigation(investigation); setShowViewsDialog(false); }} type="button">Open</button>
                  <button className="is-danger" disabled={deletingInvestigationId === investigation.id} onClick={() => deleteInvestigation(investigation)} type="button">{deletingInvestigationId === investigation.id ? "Deleting…" : "Delete"}</button>
                </div>
              </article>
            ))}
          </section>
          <section className="inventory-save-form" aria-label="Save current view">
            <div className="inventory-dialog-section-heading">
              <h3>{selectedInvestigationId ? "Update view" : "Save current view"}</h3>
              {selectedInvestigationId ? <button className="inventory-text-button" onClick={() => { setSelectedInvestigationId(null); setInvestigationName(""); setInvestigationDescription(""); }} type="button">Save as new</button> : null}
            </div>
            <label className={FILTER_LABEL_CLASS}>Name<input className={FILTER_INPUT_CLASS} onChange={(event) => setInvestigationName(event.target.value)} placeholder="Readable finance exposure" value={investigationName} /></label>
            <label className={FILTER_LABEL_CLASS}>Notes<textarea className={`${FILTER_INPUT_CLASS} min-h-[88px]`} onChange={(event) => setInvestigationDescription(event.target.value)} placeholder="Purpose or handoff notes" value={investigationDescription} /></label>
            <div className="inventory-view-summary"><strong>Current scope</strong><code>{currentInvestigationSummary() || "All records"}</code><span>{activeTabCopy.label} · {activeRunCount === 0 ? "all runs" : `${activeRunCount} runs`}</span></div>
            <div className="inventory-inline-actions">
              <button className="inventory-button-primary" disabled={savingInvestigation} onClick={selectedInvestigationId ? updateInvestigation : saveInvestigation} type="button">{savingInvestigation ? "Saving…" : selectedInvestigationId ? "Update view" : "Save view"}</button>
            </div>
          </section>
        </div>
      </Dialog>

      <datalist id="inventory-extension-options">{extensions.map((facet) => <option key={facet.ext} value={facet.ext}>{facet.count}</option>)}</datalist>
      <div aria-live="polite" className={`inventory-copy-notice ${copiedNotice ? "is-visible" : ""}`} role="status">{copiedNotice}</div>
    </section>
  );
}
