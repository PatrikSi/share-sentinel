import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AccessCapabilityCell, type AccessCapabilities } from "@/components/access-capability-cell";
import { ColumnPicker } from "@/components/column-picker";
import { Dialog } from "@/components/dialog";
import { ExposureBadge, ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { parseInventoryQuery, type InventoryQueryClause, type InventoryQueryField, type InventoryQueryGroup } from "@/lib/inventory-query";
import {
  assessedIdentity,
  collectionContextProvider,
  collectionIsPartial,
  collectionLimitationLabel,
  collectionModeLabel,
  exposureLabel,
  metadataString,
  normalizedProvider,
  providerLabel,
  resourceTypeLabel,
  safeExternalUrl,
  type CollectionContext,
  type ProviderMetadata,
} from "@/lib/provider-context";

type Project = { id: string; name: string };
type RunOption = { id: string; name: string; status: string; created_at: string; collection_context?: CollectionContext | null };
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
  endpoint_metadata?: ProviderMetadata | null;
  resource_name: string;
  access_level: string;
  access_capabilities: AccessCapabilities | null;
  share_type: string;
  resource_type?: string | null;
  provider?: string | null;
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
  provider_item_id?: string | null;
  provider_parent_id?: string | null;
  web_url?: string | null;
  mime_type?: string | null;
  deleted?: boolean;
  metadata?: ProviderMetadata | null;
  exposure?: string | null;
  exposure_evidence?: ProviderMetadata | null;
};

type InventoryResource = {
  id: number;
  run_id: string;
  run_name: string;
  endpoint_key: string;
  hostname: string | null;
  endpoint_metadata?: ProviderMetadata | null;
  name: string;
  remark: string | null;
  access_level: string;
  access_capabilities: AccessCapabilities | null;
  share_type: string;
  resource_type?: string | null;
  provider?: string | null;
  provider_resource_id?: string | null;
  web_url?: string | null;
  metadata?: ProviderMetadata | null;
  exposure?: string | null;
  exposure_evidence?: ProviderMetadata | null;
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
  provider?: string | null;
  metadata?: ProviderMetadata | null;
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
    provider_filter?: string;
    resource_type_filter?: string;
    exposure_filter?: string;
    include_deleted?: boolean;
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
  | "provider"
  | "assessment_scope"
  | "resource_type"
  | "exposure"
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
  | "file_attributes"
  | "web_url"
  | "mime_type"
  | "provider_item_id"
  | "provider_parent_id"
  | "site_id"
  | "drive_id"
  | "deleted";
type ResourceColumnKey =
  | "name"
  | "provider"
  | "assessment_scope"
  | "resource_type"
  | "exposure"
  | "access_level"
  | "endpoint_key"
  | "hostname"
  | "item_count"
  | "run_name"
  | "run_id"
  | "remark"
  | "web_url"
  | "provider_resource_id"
  | "site_id";
type EndpointColumnKey =
  | "endpoint_key"
  | "provider"
  | "assessment_scope"
  | "site_name"
  | "web_url"
  | "site_id"
  | "tenant_id"
  | "hostname"
  | "ip"
  | "domain"
  | "smb_signing"
  | "resource_count"
  | "item_count"
  | "run_name"
  | "run_id";
type QueryFilterReflection = {
  value: string;
  modeLabel: string | null;
  summary: string | null;
  selectValue: string;
};
type Density = "compact" | "comfortable";
type CellFilterField = InventoryQueryField | null;

const DEFAULT_ITEM_COLUMNS: ItemColumnKey[] = ["path", "name", "provider", "resource_name", "access_level", "exposure", "size_bytes", "mtime", "run_name"];
const DEFAULT_RESOURCE_COLUMNS: ResourceColumnKey[] = ["name", "provider", "resource_type", "exposure", "access_level", "hostname", "item_count", "run_name"];
const DEFAULT_ENDPOINT_COLUMNS: EndpointColumnKey[] = ["endpoint_key", "provider", "site_name", "hostname", "resource_count", "item_count", "run_name"];

const ITEM_COLUMN_OPTIONS: Array<{ key: ItemColumnKey; label: string }> = [
  { key: "path", label: "Path" },
  { key: "name", label: "Name" },
  { key: "resource_name", label: "Share / Library" },
  { key: "provider", label: "Source" },
  { key: "assessment_scope", label: "Assessment Scope" },
  { key: "resource_type", label: "Resource Type" },
  { key: "exposure", label: "Exposure" },
  { key: "share_type", label: "Legacy Share Type" },
  { key: "access_level", label: "Observed Access" },
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Site / Host" },
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
  { key: "web_url", label: "Canonical URL" },
  { key: "mime_type", label: "MIME Type" },
  { key: "provider_item_id", label: "Provider Item ID" },
  { key: "provider_parent_id", label: "Provider Parent ID" },
  { key: "site_id", label: "SharePoint Site ID" },
  { key: "drive_id", label: "SharePoint Drive ID" },
  { key: "deleted", label: "Deleted" },
];
const RESOURCE_COLUMN_OPTIONS: Array<{ key: ResourceColumnKey; label: string }> = [
  { key: "name", label: "Share / Library" },
  { key: "provider", label: "Source" },
  { key: "assessment_scope", label: "Assessment Scope" },
  { key: "resource_type", label: "Resource Type" },
  { key: "exposure", label: "Exposure" },
  { key: "access_level", label: "Observed Access" },
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Site / Host" },
  { key: "item_count", label: "Items" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
  { key: "remark", label: "Remark" },
  { key: "web_url", label: "Canonical URL" },
  { key: "provider_resource_id", label: "Provider Resource ID" },
  { key: "site_id", label: "SharePoint Site ID" },
];
const ENDPOINT_COLUMN_OPTIONS: Array<{ key: EndpointColumnKey; label: string }> = [
  { key: "endpoint_key", label: "Source Key" },
  { key: "provider", label: "Source" },
  { key: "assessment_scope", label: "Assessment Scope" },
  { key: "site_name", label: "Site / Host" },
  { key: "web_url", label: "Canonical URL" },
  { key: "site_id", label: "SharePoint Site ID" },
  { key: "tenant_id", label: "Tenant ID" },
  { key: "hostname", label: "Hostname / Domain" },
  { key: "ip", label: "IP" },
  { key: "domain", label: "Domain" },
  { key: "smb_signing", label: "Signing" },
  { key: "resource_count", label: "Resources" },
  { key: "item_count", label: "Items" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
];
const QUERYABLE_FIELDS: InventoryQueryField[] = ["search", "endpoint", "share", "path", "ext", "access", "provider", "source", "resource_type", "exposure"];
const MAX_EXPLICIT_RUN_SELECTIONS = 100;
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
const EXPOSURE_QUERY_ALIASES: Record<string, string> = {
  user_visible: "USER_VISIBLE",
  uservisible: "USER_VISIBLE",
  broad_internal: "BROAD_INTERNAL",
  broadinternal: "BROAD_INTERNAL",
  external: "EXTERNAL",
  anonymous: "ANONYMOUS",
  anyone: "ANONYMOUS",
  restricted: "RESTRICTED",
  unknown: "UNKNOWN",
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
  if (field === "provider" || field === "source" || field === "resource_type") return trimmed.toLowerCase().replaceAll(" ", "_");
  if (field === "exposure") {
    const normalized = trimmed.toLowerCase().replaceAll(" ", "_");
    return EXPOSURE_QUERY_ALIASES[normalized] || trimmed.toUpperCase();
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
    provider: { value: "", modeLabel: null, summary: null, selectValue: "" },
    source: { value: "", modeLabel: null, summary: null, selectValue: "" },
    resource_type: { value: "", modeLabel: null, summary: null, selectValue: "" },
    exposure: { value: "", modeLabel: null, summary: null, selectValue: "" },
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
    label: "Resources",
    description: "Compare SMB shares, NFS exports, and SharePoint libraries with access and evidence-based exposure context.",
    emptyTitle: "No resources match these filters.",
    emptyBody: "Try a broader endpoint or access filter, or compare a different run scope.",
  },
  endpoints: {
    label: "Sites & Endpoints",
    description: "Review source scope as hosts or SharePoint sites, then pivot into the resources and items behind each source.",
    emptyTitle: "No sites or endpoints match these filters.",
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
  provider: "Source",
  source: "Run Source",
  resource_type: "Resource Type",
  exposure: "Exposure",
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

function normalizeRunSelection(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].slice(0, MAX_EXPLICIT_RUN_SELECTIONS);
}

function readInitialRunSelection(): { ids: string[]; truncated: boolean } {
  const rawIds = readInitialSearchParam("runs")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const uniqueIds = [...new Set(rawIds)];
  return {
    ids: uniqueIds.slice(0, MAX_EXPLICIT_RUN_SELECTIONS),
    truncated: uniqueIds.length > MAX_EXPLICIT_RUN_SELECTIONS,
  };
}

function readInitialRuns(): string[] {
  return readInitialRunSelection().ids;
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

function readStoredDensity(): Density {
  if (typeof window === "undefined") return "compact";
  try {
    return localStorage.getItem("share_sentinel_inventory_density") === "comfortable" ? "comfortable" : "compact";
  } catch {
    return "compact";
  }
}

function persistInventoryPreference(storageKey: string, value: string): string | null {
  try {
    localStorage.setItem(storageKey, value);
    return null;
  } catch (error) {
    const reason = error instanceof Error ? error.message : "Browser storage is unavailable.";
    return `${reason} This display preference will last only until the page is closed.`;
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
  content?: ReactNode;
  filterField?: CellFilterField;
  filterScopeLabel?: string;
  filterValue?: string;
  mono?: boolean;
  badge?: "neutral" | "positive" | "warning";
  onFilter: (field: InventoryQueryField, value: string, negated: boolean) => void;
  onCopy: (value: string, label: string) => void;
};

function InventoryCell({ text, label, content, filterField = null, filterScopeLabel, filterValue, mono = false, badge, onFilter, onCopy }: InventoryCellProps) {
  const exactValue = filterValue ?? text;
  const actionable = exactValue !== "" && exactValue !== "—";
  const filterTarget = filterScopeLabel || label;
  return (
    <div className="inventory-cell">
      {content || (
        <span
          className={`inventory-cell-text ${mono ? "is-mono" : ""} ${badge ? `inventory-value-badge is-${badge}` : ""}`}
          title={text === "—" ? undefined : text}
        >
          {text}
        </span>
      )}
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
  const [runCatalogLimited, setRunCatalogLimited] = useState(false);
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>(readInitialRuns);
  const [runScopeWarning, setRunScopeWarning] = useState<string | null>(() =>
    readInitialRunSelection().truncated
      ? `The URL selected more than ${MAX_EXPLICIT_RUN_SELECTIONS} runs. Only the first ${MAX_EXPLICIT_RUN_SELECTIONS} were applied.`
      : null,
  );

  const [activeTab, setActiveTab] = useState<Tab>(readInitialTab);
  const [query, setQuery] = useState(() => readInitialSearchParam("q"));
  const [endpointFilter, setEndpointFilter] = useState(() => readInitialSearchParam("endpoint"));
  const [shareFilter, setShareFilter] = useState(() => readInitialSearchParam("share"));
  const [pathPrefix, setPathPrefix] = useState(() => readInitialSearchParam("path"));
  const [extFilter, setExtFilter] = useState(() => readInitialSearchParam("ext"));
  const [resourceAccess, setResourceAccess] = useState(() => readInitialSearchParam("access"));
  const [providerFilter, setProviderFilter] = useState(() => readInitialSearchParam("provider"));
  const [resourceTypeFilter, setResourceTypeFilter] = useState(() => readInitialSearchParam("resourceType"));
  const [exposureFilter, setExposureFilter] = useState(() => readInitialSearchParam("exposure"));
  const [includeDeleted, setIncludeDeleted] = useState(() => readInitialSearchParam("includeDeleted") === "1");
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
  const [projectContextNonce, setProjectContextNonce] = useState(0);
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
    [
      readInitialSearchParam("endpoint"),
      readInitialSearchParam("share"),
      readInitialSearchParam("path"),
      readInitialSearchParam("ext"),
      readInitialSearchParam("access"),
      readInitialSearchParam("provider"),
      readInitialSearchParam("resourceType"),
      readInitialSearchParam("exposure"),
    ].some(Boolean),
  );
  const [showViewsDialog, setShowViewsDialog] = useState(false);
  const [density, setDensity] = useState<Density>(readStoredDensity);
  const [preferenceWarning, setPreferenceWarning] = useState<string | null>(null);
  const [copiedNotice, setCopiedNotice] = useState<string | null>(null);

  const runIdsParam = useMemo(() => selectedRunIds.join(","), [selectedRunIds]);
  const debouncedQuery = useDebouncedValue(query, 300);
  const debouncedEndpointFilter = useDebouncedValue(endpointFilter, 300);
  const debouncedShareFilter = useDebouncedValue(shareFilter, 300);
  const debouncedPathPrefix = useDebouncedValue(pathPrefix, 300);
  const debouncedExtFilter = useDebouncedValue(extFilter, 300);
  const debouncedResourceAccess = useDebouncedValue(resourceAccess, 150);
  const debouncedProviderFilter = useDebouncedValue(providerFilter, 150);
  const debouncedResourceTypeFilter = useDebouncedValue(resourceTypeFilter, 150);
  const debouncedExposureFilter = useDebouncedValue(exposureFilter, 150);
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
  const includeDeletedApplies = activeTab === "items" && includeDeleted;
  const hasGuidedFilters =
    [query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess, providerFilter, resourceTypeFilter, exposureFilter].some((value) => value.trim()) ||
    includeDeletedApplies;
  const hasActiveFilters = queryModeActive || hasGuidedFilters || selectedRunIds.length > 0;
  const eligibleRuns = useMemo(() => runs.filter((run) => run.status === "COMPLETE" || run.status === "INGESTING"), [runs]);
  const selectedRunsOutsideCatalog = useMemo(() => {
    if (!runsLoaded) return [];
    const catalogIds = new Set(runs.map((run) => run.id));
    return selectedRunIds.filter((id) => !catalogIds.has(id));
  }, [runs, runsLoaded, selectedRunIds]);
  const partialRunCount =
    selectedRunIds.length > 0
      ? eligibleRuns.filter((run) => selectedRunIds.includes(run.id) && run.status === "INGESTING").length
      : inventoryStats?.runs_ingesting || 0;
  const unavailableRunCount = runs.length - eligibleRuns.length;
  const contextScopedRuns = selectedRunIds.length > 0 ? eligibleRuns.filter((run) => selectedRunIds.includes(run.id)) : eligibleRuns;
  const scopedSharePointRuns = contextScopedRuns.filter((run) => collectionContextProvider(run.collection_context) === "sharepoint");
  const scopedCollectionModes = new Set(
    scopedSharePointRuns
      .map((run) => run.collection_context?.collection_mode)
      .filter((mode): mode is string => typeof mode === "string" && mode.length > 0),
  );
  const mixedSharePointPerspectives = scopedCollectionModes.has("tenant_inventory") && scopedCollectionModes.has("delegated_user_view");
  const incompleteCollectionRunCount = contextScopedRuns.filter((run) => collectionIsPartial(run.collection_context)).length;
  const delegatedIdentities = [
    ...new Set(
      scopedSharePointRuns
        .filter((run) => run.collection_context?.collection_mode === "delegated_user_view")
        .map((run) => assessedIdentity(run.collection_context))
        .filter((identity): identity is string => !!identity),
    ),
  ];

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
    setProviderFilter(reflections.provider.selectValue);
    setResourceTypeFilter(reflections.resource_type.selectValue);
    setExposureFilter(reflections.exposure.selectValue);
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
    setProviderFilter("");
    setResourceTypeFilter("");
    setExposureFilter("");
    setIncludeDeleted(false);
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
    if (providerFilter.trim()) group.push({ field: "provider", operator: "equals", value: providerFilter.trim(), negated: false });
    if (resourceTypeFilter.trim()) group.push({ field: "resource_type", operator: "equals", value: resourceTypeFilter.trim(), negated: false });
    if (exposureFilter.trim()) group.push({ field: "exposure", operator: "equals", value: exposureFilter.trim(), negated: false });
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
    if (field === "provider") setProviderFilter("");
    if (field === "resource_type") setResourceTypeFilter("");
    if (field === "exposure") setExposureFilter("");
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
      providerFilter.trim() ? `provider:${providerFilter.trim()}` : null,
      resourceTypeFilter.trim() ? `resource_type:${resourceTypeFilter.trim()}` : null,
      exposureFilter.trim() ? `exposure:${exposureFilter.trim()}` : null,
      includeDeleted ? "include:deleted" : null,
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
        provider_filter: providerFilter,
        resource_type_filter: resourceTypeFilter,
        exposure_filter: exposureFilter,
        include_deleted: includeDeleted,
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
    const rawSelectedRuns = Array.isArray(definition.selected_run_ids)
      ? definition.selected_run_ids.filter((value): value is string => typeof value === "string")
      : [];
    const uniqueSelectedRuns = [...new Set(rawSelectedRuns.map((value) => value.trim()).filter(Boolean))];
    const selectedRuns = normalizeRunSelection(uniqueSelectedRuns);

    setActiveTab(targetTab);
    setSelectedRunIds(selectedRuns);
    setRunScopeWarning(
      uniqueSelectedRuns.length > MAX_EXPLICIT_RUN_SELECTIONS
        ? `This saved view referenced more than ${MAX_EXPLICIT_RUN_SELECTIONS} runs. Only the first ${MAX_EXPLICIT_RUN_SELECTIONS} were applied; save the view again to update it.`
        : null,
    );
    setQuery(typeof filters.query === "string" ? filters.query : "");
    setEndpointFilter(typeof filters.endpoint_filter === "string" ? filters.endpoint_filter : "");
    setShareFilter(typeof filters.share_filter === "string" ? filters.share_filter : "");
    setPathPrefix(typeof filters.path_prefix === "string" ? filters.path_prefix : "");
    setExtFilter(typeof filters.ext_filter === "string" ? filters.ext_filter : "");
    setResourceAccess(typeof filters.resource_access === "string" ? filters.resource_access : "");
    setProviderFilter(typeof filters.provider_filter === "string" ? filters.provider_filter : "");
    setResourceTypeFilter(typeof filters.resource_type_filter === "string" ? filters.resource_type_filter : "");
    setExposureFilter(typeof filters.exposure_filter === "string" ? filters.exposure_filter : "");
    setIncludeDeleted(filters.include_deleted === true);
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
      if (providerFilter.trim()) next.set("provider", providerFilter.trim());
      if (resourceTypeFilter.trim()) next.set("resourceType", resourceTypeFilter.trim());
      if (exposureFilter.trim()) next.set("exposure", exposureFilter.trim());
    }
    if (includeDeleted) next.set("includeDeleted", "1");
    setSearchParams(next, { replace: true });
  }, [activeTab, appliedInventoryQuery, endpointFilter, exposureFilter, extFilter, includeDeleted, pathPrefix, providerFilter, query, queryModeActive, resourceAccess, resourceTypeFilter, selectedRunIds, setSearchParams, shareFilter]);

  useEffect(() => {
    const persistenceError = persistInventoryPreference("share_sentinel_inventory_item_columns", JSON.stringify(itemColumns));
    if (persistenceError) setPreferenceWarning(persistenceError);
  }, [itemColumns]);

  useEffect(() => {
    const persistenceError = persistInventoryPreference("share_sentinel_inventory_resource_columns", JSON.stringify(resourceColumns));
    if (persistenceError) setPreferenceWarning(persistenceError);
  }, [resourceColumns]);

  useEffect(() => {
    const persistenceError = persistInventoryPreference("share_sentinel_inventory_endpoint_columns", JSON.stringify(endpointColumns));
    if (persistenceError) setPreferenceWarning(persistenceError);
  }, [endpointColumns]);

  useEffect(() => {
    const persistenceError = persistInventoryPreference("share_sentinel_inventory_density", density);
    if (persistenceError) setPreferenceWarning(persistenceError);
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
    setRunCatalogLimited(false);
    const initialRunSelection = readInitialRunSelection();
    setSelectedRunIds(initialRunSelection.ids);
    setRunScopeWarning(
      initialRunSelection.truncated
        ? `The URL selected more than ${MAX_EXPLICIT_RUN_SELECTIONS} runs. Only the first ${MAX_EXPLICIT_RUN_SELECTIONS} were applied.`
        : null,
    );
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
      .catch((err) => {
        if (cancelled) return;
        setProjectRole(null);
        setProjectRoleStatus("error");
        if (!isAbortError(err)) {
          setError(
            `Project access could not be confirmed; import actions remain disabled. ${err instanceof Error ? err.message : "Retry when the API is available."}`,
          );
        }
      });

    apiFetch(`/projects/${projectId}`, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) setProject(data as Project);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });

    apiFetch(`/projects/${projectId}/runs?limit=200`, { signal: controller.signal })
      .then((data) => {
        if (!cancelled) {
          setRuns((data?.items || []) as RunOption[]);
          setRunCatalogLimited(!!data?.next_cursor);
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
  }, [projectContextNonce, projectId]);

  useEffect(() => {
    if (!runsLoaded) return;
    const knownIneligibleIds = new Set(
      runs.filter((run) => run.status !== "COMPLETE" && run.status !== "INGESTING").map((run) => run.id),
    );
    const selectedKnownIneligible = selectedRunIds.filter((id) => knownIneligibleIds.has(id));
    if (selectedKnownIneligible.length === 0) return;
    setSelectedRunIds((current) => current.filter((id) => !knownIneligibleIds.has(id)));
    setRunScopeWarning(
      `${selectedKnownIneligible.length} selected run${selectedKnownIneligible.length === 1 ? " was" : "s were"} removed because pending, uploaded, or failed runs do not have queryable inventory.`,
    );
  }, [runs, runsLoaded, selectedRunIds]);

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
  }, [activeTab, appliedInventoryQuery, projectId, runIdsParam, debouncedQuery, debouncedEndpointFilter, debouncedShareFilter, debouncedPathPrefix, debouncedExtFilter, debouncedResourceAccess, debouncedProviderFilter, debouncedResourceTypeFilter, debouncedExposureFilter, includeDeleted]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    const queryParams = new URLSearchParams({ limit: "200" });
    if (cursor) queryParams.set("cursor", cursor);
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);
    if (activeTab === "items" && includeDeleted) queryParams.set("include_deleted", "true");
    if (queryModeActive) queryParams.set("query_dsl", appliedInventoryQuery.trim());
    if (!queryModeActive) {
      const guidedGroup: InventoryQueryGroup = [];
      if (debouncedQuery.trim()) guidedGroup.push({ field: "search", operator: "contains", value: debouncedQuery.trim(), negated: false });
      if (debouncedEndpointFilter.trim()) guidedGroup.push({ field: "endpoint", operator: "contains", value: debouncedEndpointFilter.trim(), negated: false });
      if (debouncedShareFilter.trim()) guidedGroup.push({ field: "share", operator: "contains", value: debouncedShareFilter.trim(), negated: false });
      if (debouncedPathPrefix.trim()) guidedGroup.push({ field: "path", operator: "startswith", value: debouncedPathPrefix.trim(), negated: false });
      if (debouncedExtFilter.trim()) guidedGroup.push({ field: "ext", operator: "equals", value: debouncedExtFilter.trim(), negated: false });
      if (debouncedResourceAccess.trim()) guidedGroup.push({ field: "access", operator: "equals", value: debouncedResourceAccess.trim(), negated: false });
      if (debouncedProviderFilter.trim()) guidedGroup.push({ field: "provider", operator: "equals", value: debouncedProviderFilter.trim(), negated: false });
      if (debouncedResourceTypeFilter.trim()) guidedGroup.push({ field: "resource_type", operator: "equals", value: debouncedResourceTypeFilter.trim(), negated: false });
      if (debouncedExposureFilter.trim()) guidedGroup.push({ field: "exposure", operator: "equals", value: debouncedExposureFilter.trim(), negated: false });
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
    debouncedProviderFilter,
    debouncedQuery,
    debouncedResourceAccess,
    debouncedResourceTypeFilter,
    debouncedShareFilter,
    debouncedExposureFilter,
    includeDeleted,
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

  function assessmentScopeCell(runId: string, label: string): ReactNode {
    const context = runs.find((run) => run.id === runId)?.collection_context;
    if (!context || Object.keys(context).length === 0) {
      return <InventoryCell text="Context not recorded" label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    }
    const mode = collectionModeLabel(context.collection_mode);
    const identity = assessedIdentity(context);
    const limitation = collectionLimitationLabel(context);
    const text = `${mode}${identity ? ` · ${identity}` : ""}${limitation ? ` · ${limitation}` : ""}`;
    return (
      <InventoryCell
        content={(
          <span className="inventory-assessment-scope" title={text}>
            <strong>{mode}</strong>
            {identity ? <small>{identity}</small> : null}
            {limitation ? <small className="is-partial">{limitation}</small> : null}
          </span>
        )}
        label={label}
        onCopy={copyExactValue}
        onFilter={applyCellFilter}
        text={text}
      />
    );
  }

  function itemCell(row: InventoryItem, column: ItemColumnKey): ReactNode {
    const label = ITEM_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    const provider = normalizedProvider(row.provider, row.share_type, row.resource_type);
    const resourceType = row.resource_type || (provider === "sharepoint" ? "sharepoint_library" : `${provider}_share`);
    const exposure = row.exposure || (provider === "sharepoint" ? "UNKNOWN" : null);
    const siteId = metadataString(row.metadata, "site_id", "siteId");
    const siteName = metadataString(row.endpoint_metadata, "display_name", "displayName", "site_name", "siteName", "name");
    const driveId = metadataString(row.metadata, "drive_id", "driveId");
    const webUrl = safeExternalUrl(row.web_url);
    if (column === "path") return <InventoryCell text={row.path} label={label} filterField="path" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "name") {
      return (
        <InventoryCell
          content={(
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="inventory-cell-text" title={row.name}>{row.name}</span>
              {row.deleted ? <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold text-slate-700 dark:bg-slate-700 dark:text-slate-200">Deleted</span> : null}
            </span>
          )}
          filterField="search"
          filterScopeLabel="any searchable item field"
          label={label}
          onCopy={copyExactValue}
          onFilter={applyCellFilter}
          text={row.name}
        />
      );
    }
    if (column === "resource_name") {
      return (
        <InventoryCell
          content={provider === "sharepoint" && siteName ? (
            <span className="inventory-assessment-scope" title={`${row.resource_name} · ${siteName}`}>
              <strong>{row.resource_name}</strong>
              <small>Site: {siteName}</small>
            </span>
          ) : undefined}
          filterField="share"
          label={label}
          onCopy={copyExactValue}
          onFilter={applyCellFilter}
          text={row.resource_name}
        />
      );
    }
    if (column === "provider") return <InventoryCell content={<ProviderBadge provider={provider} />} text={providerLabel(provider)} filterField="provider" filterValue={provider} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "assessment_scope") return assessmentScopeCell(row.run_id, label);
    if (column === "resource_type") return <InventoryCell text={resourceTypeLabel(resourceType)} filterField="resource_type" filterValue={resourceType} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "exposure") return exposure ? <InventoryCell content={<ExposureBadge evidence={row.exposure_evidence} exposure={exposure} />} text={exposureLabel(exposure)} filterField="exposure" filterValue={exposure} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} /> : <InventoryCell text="—" label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "share_type") return <InventoryCell text={row.share_type.toUpperCase()} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "access_level") {
      return (
        <AccessCapabilityCell
          accessLevel={row.access_level}
          capabilities={row.access_capabilities}
          evidenceScope={provider === "sharepoint" ? "Library scope" : "Share sample"}
          label={label}
          onCopy={copyExactValue}
          onFilter={(value, negated) => applyCellFilter("access", value, negated)}
        />
      );
    }
    if (column === "endpoint_key") return <InventoryCell text={row.endpoint_key} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "hostname") return <InventoryCell text={siteName || row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="site or host" onFilter={applyCellFilter} onCopy={copyExactValue} />;
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
    if (column === "web_url") return webUrl ? <InventoryCell content={<a className="inventory-external-link" href={webUrl} rel="noreferrer" target="_blank">Open item <span aria-hidden="true">↗</span></a>} text={webUrl} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} /> : <InventoryCell text={row.web_url ? "Unsafe URL blocked" : "—"} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "mime_type") return <InventoryCell text={row.mime_type || "—"} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "provider_item_id") return <InventoryCell text={row.provider_item_id || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "provider_parent_id") return <InventoryCell text={row.provider_parent_id || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "site_id") return <InventoryCell text={siteId || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "drive_id") return <InventoryCell text={driveId || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "deleted") return <InventoryCell text={row.deleted ? "Deleted" : "Current"} label={label} badge={row.deleted ? "warning" : "positive"} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.is_dir ? "Directory" : "File"} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  function resourceCell(row: InventoryResource, column: ResourceColumnKey): ReactNode {
    const label = RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    const provider = normalizedProvider(row.provider, row.share_type, row.resource_type);
    const resourceType = row.resource_type || (provider === "sharepoint" ? "sharepoint_library" : `${provider}_share`);
    const exposure = row.exposure || (provider === "sharepoint" ? "UNKNOWN" : null);
    const siteId = metadataString(row.metadata, "site_id", "siteId");
    const siteName = metadataString(row.endpoint_metadata, "display_name", "displayName", "site_name", "siteName", "name");
    const webUrl = safeExternalUrl(row.web_url);
    if (column === "name") return <InventoryCell text={row.name} label={label} filterField="share" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "provider") return <InventoryCell content={<ProviderBadge provider={provider} />} text={providerLabel(provider)} filterField="provider" filterValue={provider} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "assessment_scope") return assessmentScopeCell(row.run_id, label);
    if (column === "resource_type") return <InventoryCell text={resourceTypeLabel(resourceType)} filterField="resource_type" filterValue={resourceType} label={label} badge="neutral" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "exposure") return exposure ? <InventoryCell content={<ExposureBadge evidence={row.exposure_evidence} exposure={exposure} />} text={exposureLabel(exposure)} filterField="exposure" filterValue={exposure} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} /> : <InventoryCell text="—" label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
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
    if (column === "hostname") return <InventoryCell text={siteName || row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="site or host" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "item_count") return <InventoryCell text={row.item_count.toLocaleString()} filterValue={String(row.item_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_name") return <InventoryCell text={row.run_name} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_id") return <InventoryCell text={row.run_id} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "web_url") return webUrl ? <InventoryCell content={<a className="inventory-external-link" href={webUrl} rel="noreferrer" target="_blank">Open resource <span aria-hidden="true">↗</span></a>} text={webUrl} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} /> : <InventoryCell text={row.web_url ? "Unsafe URL blocked" : "—"} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "provider_resource_id") return <InventoryCell text={row.provider_resource_id || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "site_id") return <InventoryCell text={siteId || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.remark || "—"} label={label} filterField="search" filterScopeLabel="any searchable resource field" onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  function endpointCell(row: InventoryEndpoint, column: EndpointColumnKey): ReactNode {
    const label = ENDPOINT_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column;
    const siteName = metadataString(row.metadata, "display_name", "displayName", "site_name", "siteName", "name");
    const rawWebUrl = metadataString(row.metadata, "web_url", "webUrl");
    const webUrl = safeExternalUrl(rawWebUrl);
    const siteId = metadataString(row.metadata, "site_id", "siteId");
    const provider = normalizedProvider(row.provider, metadataString(row.metadata, "provider"), siteId ? "sharepoint" : "network");
    const tenantId = metadataString(row.metadata, "tenant_id", "tenantId");
    if (column === "endpoint_key") return <InventoryCell text={row.endpoint_key} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "provider") return <InventoryCell content={<ProviderBadge provider={provider} />} text={providerLabel(provider)} filterField={provider === "network" ? undefined : "provider"} filterValue={provider} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "assessment_scope") return assessmentScopeCell(row.run_id, label);
    if (column === "site_name") return <InventoryCell text={siteName || row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="site or endpoint" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "web_url") return webUrl ? <InventoryCell content={<a className="inventory-external-link" href={webUrl} rel="noreferrer" target="_blank">Open site <span aria-hidden="true">↗</span></a>} text={webUrl} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} /> : <InventoryCell text={rawWebUrl ? "Unsafe URL blocked" : "—"} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "site_id") return <InventoryCell text={siteId || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "tenant_id") return <InventoryCell text={tenantId || "—"} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "hostname") return <InventoryCell text={row.hostname || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "ip") return <InventoryCell text={row.ip || "—"} label={label} filterField="endpoint" filterScopeLabel="endpoint key, hostname, or IP" mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "domain") return <InventoryCell text={row.domain || "—"} label={label} filterField="search" filterScopeLabel="any searchable source field" onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "smb_signing") return <InventoryCell text={row.smb_signing || "—"} label={label} badge={row.smb_signing?.toLowerCase() === "required" ? "positive" : "neutral"} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "resource_count") return <InventoryCell text={row.resource_count.toLocaleString()} filterValue={String(row.resource_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "item_count") return <InventoryCell text={row.item_count.toLocaleString()} filterValue={String(row.item_count)} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    if (column === "run_name") return <InventoryCell text={row.run_name} label={label} onFilter={applyCellFilter} onCopy={copyExactValue} />;
    return <InventoryCell text={row.run_id} label={label} mono onFilter={applyCellFilter} onCopy={copyExactValue} />;
  }

  const activeTabCopy = INVENTORY_TAB_COPY[activeTab];
  const guidedFilterCount = queryModeActive
    ? QUERYABLE_FIELDS.filter((field) => !!queryFilterReflections[field].summary).length + (includeDeletedApplies ? 1 : 0)
    : [query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess, providerFilter, resourceTypeFilter, exposureFilter].filter((value) => value.trim()).length + (includeDeletedApplies ? 1 : 0);
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
          <p>Compare files, folders, shares, libraries, sites, and hosts without losing source or assessment context.</p>
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
          <p className="mt-1">The result table may still be usable, but project scope, run choices, saved views, or permissions may be incomplete.</p>
          <button
            className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold"
            onClick={() => {
              setError(null);
              setProjectContextNonce((current) => current + 1);
            }}
            type="button"
          >
            Retry project context
          </button>
        </StatusBanner>
      ) : null}
      {partialRunCount > 0 ? (
        <StatusBanner tone="warning" title="Inventory is still changing">
          <p>
            {partialRunCount} run{partialRunCount === 1 ? " is" : "s are"} still ingesting. Results include committed records and may be incomplete until ingestion finishes.
          </p>
        </StatusBanner>
      ) : null}
      {incompleteCollectionRunCount > 0 ? (
        <StatusBanner tone="warning" title="Some collection coverage is incomplete">
          <p>
            {incompleteCollectionRunCount} run{incompleteCollectionRunCount === 1 ? " reports" : "s report"} partial, truncated, or failed source discovery. Results remain queryable, but absence from this view is not evidence that an object does not exist.
          </p>
        </StatusBanner>
      ) : null}
      {mixedSharePointPerspectives ? (
        <StatusBanner tone="warning" title="Mixed SharePoint assessment perspectives">
          <p>
            This scope combines application tenant inventory with delegated user-visible results. Treat it as a union of observations, not as one authoritative visibility or exposure statement. Select individual runs to compare perspectives safely.
          </p>
        </StatusBanner>
      ) : delegatedIdentities.length > 0 ? (
        <StatusBanner tone="info" title="Delegated SharePoint visibility scope">
          <p>
            Results include resources visible to {delegatedIdentities.slice(0, 3).join(", ")}
            {delegatedIdentities.length > 3 ? ` and ${delegatedIdentities.length - 3} more assessed identities` : ""}. User-visible does not mean public, broad internal, external, or anonymous.
          </p>
        </StatusBanner>
      ) : null}
      {runScopeWarning ? (
        <StatusBanner tone="warning" title="Run scope was limited">
          <p>{runScopeWarning}</p>
        </StatusBanner>
      ) : null}
      {runCatalogLimited ? (
        <StatusBanner tone="info" title="Run selector shows recent history">
          <p>
            The selector is bounded to the 200 most recent runs. Leave every run unchecked to query all eligible runs, including older history.
          </p>
        </StatusBanner>
      ) : null}
      {preferenceWarning ? (
        <StatusBanner tone="warning" title="Display preferences are not persistent">
          <p>{preferenceWarning}</p>
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
              placeholder={activeTab === "items" ? "Search file, path, library, share, site, or host" : activeTab === "resources" ? "Search library, share, site, host, or remark" : "Search site, hostname, address, domain, or key"}
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
                  <p className="inventory-popover-copy">
                    The selector shows up to 200 recent runs. No selection queries every eligible run, including older history. Explicit scopes support up to {MAX_EXPLICIT_RUN_SELECTIONS} runs ({activeRunCount} selected).
                  </p>
                </div>
                <button className="inventory-text-button" disabled={activeRunCount === 0} onClick={() => setSelectedRunIds([])} type="button">
                  Clear
                </button>
              </div>
              <div className="inventory-run-list">
                {eligibleRuns.length === 0 ? <p className="inventory-empty-copy">No completed or ingesting runs are present in the recent catalog.</p> : null}
                {unavailableRunCount > 0 ? <p className="inventory-run-note">{unavailableRunCount} pending, uploaded, or failed run{unavailableRunCount === 1 ? " is" : "s are"} excluded because they have no queryable inventory.</p> : null}
                {selectedRunsOutsideCatalog.length > 0 ? (
                  <p className="inventory-run-note">
                    {selectedRunsOutsideCatalog.length} selected run{selectedRunsOutsideCatalog.length === 1 ? " is" : "s are"} outside the recent catalog. The explicit ID scope is preserved; uncheck below to remove it.
                  </p>
                ) : null}
                {selectedRunsOutsideCatalog.map((selectedRunId) => (
                  <label className="inventory-run-option" key={`older:${selectedRunId}`}>
                    <input
                      checked
                      onChange={() => {
                        setSelectedRunIds((current) => current.filter((id) => id !== selectedRunId));
                        setRunScopeWarning(null);
                      }}
                      type="checkbox"
                    />
                    <span>
                      <strong>Selected run outside recent history</strong>
                      <small>{selectedRunId}</small>
                    </span>
                  </label>
                ))}
                {eligibleRuns.map((run) => (
                  <label className="inventory-run-option" key={run.id}>
                    <input
                      checked={selectedRunIds.includes(run.id)}
                      disabled={!selectedRunIds.includes(run.id) && activeRunCount >= MAX_EXPLICIT_RUN_SELECTIONS}
                      onChange={(event) =>
                        setSelectedRunIds((current) => {
                          if (!event.target.checked) {
                            setRunScopeWarning(null);
                            return current.filter((id) => id !== run.id);
                          }
                          if (current.includes(run.id)) return current;
                          if (current.length >= MAX_EXPLICIT_RUN_SELECTIONS) {
                            setRunScopeWarning(
                              `Explicit run scope is limited to ${MAX_EXPLICIT_RUN_SELECTIONS} runs. Clear the selection to query every eligible run, or remove one before adding another.`,
                            );
                            return current;
                          }
                          setRunScopeWarning(null);
                          return [...current, run.id];
                        })
                      }
                      type="checkbox"
                    />
                    <span>
                      <strong>{run.name}</strong>
                      <small>{run.status.replaceAll("_", " ")} · {new Date(run.created_at).toLocaleString()}</small>
                      {run.collection_context && Object.keys(run.collection_context).length > 0 ? (
                        <small>
                          {providerLabel(collectionContextProvider(run.collection_context))} · {collectionModeLabel(run.collection_context.collection_mode)}
                          {assessedIdentity(run.collection_context) ? ` · ${assessedIdentity(run.collection_context)}` : ""}
                          {collectionLimitationLabel(run.collection_context) ? ` · ${collectionLimitationLabel(run.collection_context)}` : ""}
                        </small>
                      ) : null}
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
                    ["provider", "Source", providerFilter],
                    ["resource_type", "Resource Type", resourceTypeFilter],
                    ["exposure", "Exposure", exposureFilter],
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
            {includeDeletedApplies ? (
              <span className="inventory-filter-chip">
                <strong>Items</strong> Include deleted
                <button aria-label="Exclude deleted items" onClick={() => setIncludeDeleted(false)} type="button">×</button>
              </span>
            ) : null}
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
                Site or endpoint
                <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)} placeholder="Site, hostname, key, or address" value={endpointFilter} />
              </label>
              <label className={FILTER_LABEL_CLASS}>
                Source
                <select className={FILTER_SELECT_CLASS} onChange={(event) => handleSimpleFilterChange(setProviderFilter, event.target.value)} value={providerFilter}>
                  <option value="">All sources</option>
                  <option value="sharepoint">SharePoint</option>
                  <option value="smb">SMB</option>
                  <option value="nfs">NFS</option>
                </select>
              </label>
              {activeTab !== "endpoints" ? (
                <label className={FILTER_LABEL_CLASS}>
                  Share or library
                  <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setShareFilter, event.target.value)} placeholder="Finance documents" value={shareFilter} />
                </label>
              ) : null}
              <label className={FILTER_LABEL_CLASS}>
                Resource type
                <select className={FILTER_SELECT_CLASS} onChange={(event) => handleSimpleFilterChange(setResourceTypeFilter, event.target.value)} value={resourceTypeFilter}>
                  <option value="">All resource types</option>
                  <option value="sharepoint_library">SharePoint library</option>
                  <option value="smb_share">SMB share</option>
                  <option value="nfs_share">NFS export</option>
                </select>
              </label>
              <label className={FILTER_LABEL_CLASS}>
                Exposure
                <select className={FILTER_SELECT_CLASS} onChange={(event) => handleSimpleFilterChange(setExposureFilter, event.target.value)} value={exposureFilter}>
                  <option value="">Any exposure state</option>
                  <option value="ANONYMOUS">Anonymous access</option>
                  <option value="EXTERNAL">External access</option>
                  <option value="BROAD_INTERNAL">Broad internal</option>
                  <option value="USER_VISIBLE">User-visible (not public)</option>
                  <option value="RESTRICTED">Restricted</option>
                  <option value="UNKNOWN">Unknown</option>
                </select>
              </label>
              {activeTab === "items" ? (
                <>
                  <label className={FILTER_LABEL_CLASS}>
                    Path starts with
                    <input className={FILTER_INPUT_CLASS} onChange={(event) => handleSimpleFilterChange(setPathPrefix, event.target.value)} placeholder="/Finance/Quarterly or \\Finance\\Quarterly" value={pathPrefix} />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Extension
                    <input className={FILTER_INPUT_CLASS} list="inventory-extension-options" onChange={(event) => handleSimpleFilterChange(setExtFilter, event.target.value)} placeholder=".pst" value={extFilter} />
                  </label>
                </>
              ) : null}
              {activeTab === "items" ? (
                <label className={`${FILTER_LABEL_CLASS} flex items-center gap-2 self-end rounded-md border border-slate-300 px-3 py-2 dark:border-slate-700`}>
                  <input checked={includeDeleted} className="accent-emerald-600" onChange={(event) => setIncludeDeleted(event.target.checked)} type="checkbox" />
                  Include deleted records
                </label>
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
                placeholder={'provider = "sharepoint" AND exposure = "ANONYMOUS"'}
                value={inventoryQueryInput}
              />
              <p id="inventory-query-help">Fields: search, endpoint, share, path, ext, access, provider, source, resource_type, exposure. Operators: = exact, ~ contains, ^ starts with, AND, OR, ! not.</p>
            </div>
            <div className="inventory-query-actions">
              <button className="inventory-button-primary" onClick={handleInventoryQueryApply} type="button">Apply query</button>
              <button className="inventory-button-secondary" onClick={() => setInventoryQueryInput('provider = "sharepoint" AND exposure = "ANONYMOUS"')} type="button">Example</button>
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
                <tbody>{items.map((row) => <tr className={row.deleted ? "is-deleted" : undefined} key={`${row.run_id}-${row.id}`}>{itemColumns.map((column) => <td key={`${row.id}-${column}`}>{itemCell(row, column)}</td>)}</tr>)}</tbody>
              </table>
            ) : null}
            {activeTab === "resources" ? (
              <table className="inventory-table">
                <caption className="sr-only">Shares, exports, and document libraries in the selected inventory scope</caption>
                <thead><tr>{resourceColumns.map((column) => <th key={column} scope="col"><span>{RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>{resourceColumns.length > 1 ? <button aria-label={`Hide ${column} column`} onClick={() => toggleResourceColumn(column)} title="Hide column" type="button">×</button> : null}</th>)}</tr></thead>
                <tbody>{resources.map((row) => <tr key={`${row.run_id}-${row.id}`}>{resourceColumns.map((column) => <td key={`${row.id}-${column}`}>{resourceCell(row, column)}</td>)}</tr>)}</tbody>
              </table>
            ) : null}
            {activeTab === "endpoints" ? (
              <table className="inventory-table">
                <caption className="sr-only">SharePoint sites and network endpoints in the selected inventory scope</caption>
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
