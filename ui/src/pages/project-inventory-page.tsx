import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { parseInventoryQuery, type InventoryQueryClause, type InventoryQueryField, type InventoryQueryGroup } from "@/lib/inventory-query";

type Project = { id: string; name: string };
type RunOption = { id: string; name: string; status: string; created_at: string };
type ExtensionFacet = { ext: string; count: number };

type InventoryItem = {
  id: number;
  run_id: string;
  run_name: string;
  endpoint_key: string;
  hostname: string | null;
  ip: string | null;
  resource_name: string;
  access_level: string;
  share_type: string;
  path: string;
  name: string;
  is_dir: boolean;
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
  | "is_dir";
type ResourceColumnKey = "name" | "share_type" | "access_level" | "endpoint_key" | "hostname" | "item_count" | "run_name" | "run_id" | "remark";
type EndpointColumnKey = "endpoint_key" | "hostname" | "ip" | "domain" | "smb_signing" | "resource_count" | "item_count" | "run_name" | "run_id";
type QueryFilterReflection = {
  value: string;
  modeLabel: string | null;
  summary: string | null;
  selectValue: string;
};

const ITEM_COLUMN_OPTIONS: Array<{ key: ItemColumnKey; label: string }> = [
  { key: "path", label: "Path" },
  { key: "name", label: "Name" },
  { key: "resource_name", label: "Share" },
  { key: "share_type", label: "Share Type" },
  { key: "access_level", label: "Share Access" },
  { key: "endpoint_key", label: "Endpoint Key" },
  { key: "hostname", label: "Hostname" },
  { key: "ip", label: "IP" },
  { key: "run_name", label: "Run Name" },
  { key: "run_id", label: "Run ID" },
  { key: "is_dir", label: "Type" },
];
const RESOURCE_COLUMN_OPTIONS: Array<{ key: ResourceColumnKey; label: string }> = [
  { key: "name", label: "Share" },
  { key: "share_type", label: "Share Type" },
  { key: "access_level", label: "Access" },
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
  list_only: "list_only",
  list: "list_only",
  browse: "list_only",
  readable: "readable",
  read: "readable",
  read_only: "readable",
  read_write: "readable",
  "read-write": "readable",
  write: "readable",
  writable: "readable",
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
  const clauses = groups.flatMap((group) => group.filter((clause) => clause.field === field));
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
  path: "Path Prefix",
  ext: "Extension",
  access: "Share Access",
};

const FILTER_LABEL_CLASS = "text-xs font-semibold uppercase tracking-wider text-slate-500";
const FILTER_INPUT_CLASS =
  "mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500";
const FILTER_SELECT_CLASS =
  "mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";

export function ProjectInventoryPage() {
  const { projectId } = useParams<{ projectId: string }>();

  const [project, setProject] = useState<Project | null>(null);
  const [runs, setRuns] = useState<RunOption[]>([]);
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);

  const [activeTab, setActiveTab] = useState<Tab>("items");
  const [query, setQuery] = useState("");
  const [endpointFilter, setEndpointFilter] = useState("");
  const [shareFilter, setShareFilter] = useState("");
  const [pathPrefix, setPathPrefix] = useState("");
  const [extFilter, setExtFilter] = useState("");
  const [resourceAccess, setResourceAccess] = useState("");
  const [inventoryQueryInput, setInventoryQueryInput] = useState("");
  const [appliedInventoryQuery, setAppliedInventoryQuery] = useState("");
  const [appliedInventoryQueryGroups, setAppliedInventoryQueryGroups] = useState<InventoryQueryGroup[]>([]);

  const [extensions, setExtensions] = useState<ExtensionFacet[]>([]);
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [resources, setResources] = useState<InventoryResource[]>([]);
  const [endpoints, setEndpoints] = useState<InventoryEndpoint[]>([]);

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [savedInvestigations, setSavedInvestigations] = useState<SavedInvestigation[]>([]);
  const [selectedInvestigationId, setSelectedInvestigationId] = useState<string | null>(null);
  const [investigationName, setInvestigationName] = useState("");
  const [investigationDescription, setInvestigationDescription] = useState("");
  const [savingInvestigation, setSavingInvestigation] = useState(false);
  const [deletingInvestigationId, setDeletingInvestigationId] = useState<string | null>(null);
  const [itemColumns, setItemColumns] = useState<ItemColumnKey[]>(["path", "name", "resource_name", "share_type", "hostname", "run_name", "is_dir"]);
  const [resourceColumns, setResourceColumns] = useState<ResourceColumnKey[]>([
    "name",
    "share_type",
    "access_level",
    "hostname",
    "item_count",
    "run_name",
    "remark",
  ]);
  const [endpointColumns, setEndpointColumns] = useState<EndpointColumnKey[]>(["endpoint_key", "hostname", "domain", "resource_count", "item_count", "run_name"]);
  const [dragItemColumn, setDragItemColumn] = useState<ItemColumnKey | null>(null);
  const [dragResourceColumn, setDragResourceColumn] = useState<ResourceColumnKey | null>(null);
  const [dragEndpointColumn, setDragEndpointColumn] = useState<EndpointColumnKey | null>(null);
  const [showAdvancedQuery, setShowAdvancedQuery] = useState(false);
  const [showRunScope, setShowRunScope] = useState(false);

  const runIdsParam = useMemo(() => selectedRunIds.join(","), [selectedRunIds]);
  const endpointQuery = useMemo(() => [query.trim(), endpointFilter.trim()].filter(Boolean).join(" "), [query, endpointFilter]);
  const queryModeActive = appliedInventoryQuery.trim().length > 0;
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
  const querySummaries = QUERYABLE_FIELDS.flatMap((field) =>
    queryFilterReflections[field].summary ? [`${QUERY_FIELD_LABELS[field]}: ${queryFilterReflections[field].summary}`] : [],
  );

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

  function clearAllFilters() {
    clearSimpleFilters();
    setSelectedRunIds([]);
    setInventoryQueryInput("");
    clearAppliedInventoryQuery();
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

  async function refreshSavedInvestigations(nextSelectedId?: string | null) {
    if (!projectId) return;
    const data = await apiFetch(`/projects/${projectId}/inventory/investigations`);
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
    const name = investigationName.trim();
    if (!name) {
      setError("Name the shared investigation before saving it.");
      return;
    }

    setSavingInvestigation(true);
    setError(null);
    try {
      const created = (await apiFetch(`/projects/${projectId}/inventory/investigations`, {
        method: "POST",
        body: JSON.stringify({
          name,
          description: investigationDescription.trim() || null,
          target_tab: activeTab,
          query_text: currentInvestigationSummary(),
          definition: currentInvestigationDefinition(),
        }),
      })) as SavedInvestigation;
      await refreshSavedInvestigations(created.id);
      setSelectedInvestigationId(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save investigation.");
    } finally {
      setSavingInvestigation(false);
    }
  }

  async function updateInvestigation() {
    if (!projectId || !selectedInvestigationId) return;
    const name = investigationName.trim();
    if (!name) {
      setError("Name the shared investigation before updating it.");
      return;
    }

    setSavingInvestigation(true);
    setError(null);
    try {
      await apiFetch(`/projects/${projectId}/inventory/investigations/${selectedInvestigationId}`, {
        method: "PATCH",
        body: JSON.stringify({
          name,
          description: investigationDescription.trim() || null,
          target_tab: activeTab,
          query_text: currentInvestigationSummary(),
          definition: currentInvestigationDefinition(),
        }),
      });
      await refreshSavedInvestigations(selectedInvestigationId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update investigation.");
    } finally {
      setSavingInvestigation(false);
    }
  }

  async function deleteInvestigation(investigation: SavedInvestigation) {
    if (!projectId) return;
    if (!window.confirm(`Delete shared investigation "${investigation.name}"?`)) {
      return;
    }

    setDeletingInvestigationId(investigation.id);
    setError(null);
    try {
      await apiFetch(`/projects/${projectId}/inventory/investigations/${investigation.id}`, {
        method: "DELETE",
      });
      await refreshSavedInvestigations(investigation.id === selectedInvestigationId ? null : undefined);
      if (investigation.id === selectedInvestigationId) {
        setInvestigationName("");
        setInvestigationDescription("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete investigation.");
    } finally {
      setDeletingInvestigationId(null);
    }
  }

  useEffect(() => {
    if (queryModeActive || queryError) {
      setShowAdvancedQuery(true);
    }
  }, [queryError, queryModeActive]);

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}`)
      .then((data) => setProject(data as Project))
      .catch((err) => setError(err.message));

    apiFetchAllPages<RunOption>((cursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (cursor) query.set("cursor", cursor);
      return `/projects/${projectId}/runs?${query.toString()}`;
    })
      .then((data) => setRuns(data))
      .catch((err) => setError(err.message));

    refreshSavedInvestigations().catch((err) => setError(err instanceof Error ? err.message : "Failed to load investigations."));
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const queryParams = new URLSearchParams({ limit: "100" });
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);

    apiFetch(`/projects/${projectId}/inventory/extensions?${queryParams.toString()}`)
      .then((data) => setExtensions((data?.items || []) as ExtensionFacet[]))
      .catch((err) => setError(err.message));
  }, [projectId, runIdsParam]);

  useEffect(() => {
    setCursor(null);
    setCursorHistory([]);
  }, [activeTab, appliedInventoryQuery, projectId, runIdsParam, query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess]);

  useEffect(() => {
    if (!projectId) return;
    const queryParams = new URLSearchParams({ limit: "200" });
    if (cursor) queryParams.set("cursor", cursor);
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);
    if (queryModeActive) queryParams.set("query_dsl", appliedInventoryQuery.trim());

    if (activeTab === "items") {
      if (!queryModeActive) {
        if (query.trim()) queryParams.set("q", query.trim());
        if (endpointFilter.trim()) queryParams.set("endpoint", endpointFilter.trim());
        if (shareFilter.trim()) queryParams.set("share", shareFilter.trim());
        if (pathPrefix.trim()) queryParams.set("path_prefix", pathPrefix.trim());
        if (extFilter.trim()) queryParams.set("ext", extFilter.trim());
      }

      apiFetch(`/projects/${projectId}/inventory/items?${queryParams.toString()}`)
        .then((data) => {
          setItems((data?.items || []) as InventoryItem[]);
          setNextCursor((data?.next_cursor as string | null) || null);
        })
        .catch((err) => setError(err.message));
      return;
    }

    if (activeTab === "resources") {
      if (!queryModeActive) {
        if (query.trim()) queryParams.set("q", query.trim());
        if (endpointFilter.trim()) queryParams.set("endpoint", endpointFilter.trim());
        if (resourceAccess.trim()) queryParams.set("access_level", resourceAccess.trim());
      }

      apiFetch(`/projects/${projectId}/inventory/resources?${queryParams.toString()}`)
        .then((data) => {
          setResources((data?.items || []) as InventoryResource[]);
          setNextCursor((data?.next_cursor as string | null) || null);
        })
        .catch((err) => setError(err.message));
      return;
    }

    if (!queryModeActive && endpointQuery) queryParams.set("q", endpointQuery);
    apiFetch(`/projects/${projectId}/inventory/endpoints?${queryParams.toString()}`)
      .then((data) => {
        setEndpoints((data?.items || []) as InventoryEndpoint[]);
        setNextCursor((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [activeTab, appliedInventoryQuery, cursor, endpointFilter, endpointQuery, extFilter, pathPrefix, projectId, query, queryModeActive, resourceAccess, runIdsParam, shareFilter]);

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

  function moveInArray<T>(rows: T[], from: T, to: T): T[] {
    const sourceIndex = rows.indexOf(from);
    const targetIndex = rows.indexOf(to);
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return rows;
    const next = [...rows];
    next.splice(sourceIndex, 1);
    next.splice(targetIndex, 0, from);
    return next;
  }

  function reorderItemColumns(target: ItemColumnKey) {
    if (!dragItemColumn || dragItemColumn === target) return;
    setItemColumns((prev) => moveInArray(prev, dragItemColumn, target));
  }

  function reorderResourceColumns(target: ResourceColumnKey) {
    if (!dragResourceColumn || dragResourceColumn === target) return;
    setResourceColumns((prev) => moveInArray(prev, dragResourceColumn, target));
  }

  function reorderEndpointColumns(target: EndpointColumnKey) {
    if (!dragEndpointColumn || dragEndpointColumn === target) return;
    setEndpointColumns((prev) => moveInArray(prev, dragEndpointColumn, target));
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
    if (column === "path") return <span className="font-mono text-xs">{row.path}</span>;
    if (column === "name") return row.name;
    if (column === "resource_name") return row.resource_name;
    if (column === "share_type") return row.share_type.toUpperCase();
    if (column === "access_level") return row.access_level;
    if (column === "endpoint_key") return row.endpoint_key;
    if (column === "hostname") return row.hostname || "-";
    if (column === "ip") return row.ip || "-";
    if (column === "run_name") return row.run_name;
    if (column === "run_id") return <span className="font-mono text-xs">{row.run_id}</span>;
    return row.is_dir ? "directory" : "file";
  }

  function resourceCell(row: InventoryResource, column: ResourceColumnKey): ReactNode {
    if (column === "name") return row.name;
    if (column === "share_type") return row.share_type.toUpperCase();
    if (column === "access_level") return row.access_level;
    if (column === "endpoint_key") return row.endpoint_key;
    if (column === "hostname") return row.hostname || "-";
    if (column === "item_count") return row.item_count;
    if (column === "run_name") return row.run_name;
    if (column === "run_id") return <span className="font-mono text-xs">{row.run_id}</span>;
    return row.remark || "-";
  }

  function endpointCell(row: InventoryEndpoint, column: EndpointColumnKey): ReactNode {
    if (column === "endpoint_key") return row.endpoint_key;
    if (column === "hostname") return row.hostname || "-";
    if (column === "ip") return row.ip || "-";
    if (column === "domain") return row.domain || "-";
    if (column === "smb_signing") return row.smb_signing || "-";
    if (column === "resource_count") return row.resource_count;
    if (column === "item_count") return row.item_count;
    if (column === "run_name") return row.run_name;
    return <span className="font-mono text-xs">{row.run_id}</span>;
  }

  const activeTabCopy = INVENTORY_TAB_COPY[activeTab];

  return (
    <section className="workspace">
      <div className="workspace-header gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3 rounded-[28px] border border-slate-200 bg-[linear-gradient(135deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-5 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(135deg,rgba(15,23,42,0.96),rgba(15,23,42,0.8))]">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Inventory</p>
            <h1 className="mt-1 text-2xl font-bold tracking-tight">{project?.name || "Project Inventory"}</h1>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{project ? project.id : projectId}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              to="/projects"
            >
              Open Dashboard
            </Link>
            {projectId ? (
              <Link
                className="rounded-2xl bg-emerald-600 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-emerald-500"
                to={`/projects/${projectId}/import`}
              >
                Import Scan
              </Link>
            ) : null}
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={clearAllFilters}
              type="button"
            >
              Reset Filters
            </button>
          </div>
        </div>

        {error ? (
          <p className="rounded-2xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200">{error}</p>
        ) : null}
        {queryError ? (
          <p className="rounded-2xl bg-amber-100 p-3 text-sm text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">{queryError}</p>
        ) : null}
      </div>

      <div className="workspace-section space-y-4">
        <div className="rounded-3xl border border-slate-200 bg-white/90 p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950/60">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-500">View</p>
              <h2 className="mt-1 text-lg font-semibold">{activeTabCopy.label}</h2>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">{activeTabCopy.description}</p>
            </div>
            <div className="flex flex-wrap gap-2 text-xs">
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-medium dark:border-slate-800 dark:bg-slate-900/80">
                {activeResultCount.toLocaleString()} results
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-medium dark:border-slate-800 dark:bg-slate-900/80">
                {activeRunCount === 0 ? "All runs" : `${activeRunCount} runs`}
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-medium dark:border-slate-800 dark:bg-slate-900/80">
                {activeColumnCount.toLocaleString()} columns
              </span>
              <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 font-medium dark:border-slate-800 dark:bg-slate-900/80">
                {queryModeActive ? "Advanced query" : "Guided filters"}
              </span>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap gap-2">
            {(["items", "resources", "endpoints"] as Tab[]).map((tab) => (
              <button
                className={`rounded-2xl border px-4 py-2 text-sm font-semibold transition ${
                  activeTab === tab
                    ? "border-emerald-600 bg-emerald-50 text-emerald-900 dark:bg-emerald-900/20 dark:text-emerald-100"
                    : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-950/40 dark:text-slate-200 dark:hover:bg-slate-900/80"
                }`}
                key={tab}
                onClick={() => setActiveTab(tab)}
                type="button"
              >
                {INVENTORY_TAB_COPY[tab].label}
              </button>
            ))}
          </div>

          <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="rounded-3xl border border-slate-200 bg-white/95 p-4 dark:border-slate-800 dark:bg-slate-950/40">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-500">Guided Filters</p>
                <button
                  className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  onClick={clearSimpleFilters}
                  type="button"
                >
                  Clear Guided Filters
                </button>
              </div>

              {queryModeActive ? (
                <div className="mt-3 flex flex-wrap gap-2 text-xs">
                  {querySummaries.map((summary) => (
                    <span
                      className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-emerald-800 dark:border-emerald-900/40 dark:bg-emerald-900/20 dark:text-emerald-200"
                      key={summary}
                    >
                      {summary}
                    </span>
                  ))}
                </div>
              ) : null}

              {activeTab === "items" ? (
                <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
                  <label className={FILTER_LABEL_CLASS}>
                    Search
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="File, folder, hostname, or share"
                      value={query}
                      onChange={(event) => handleSimpleFilterChange(setQuery, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Endpoint
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="fs-01 or 10.0.0.14"
                      value={endpointFilter}
                      onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Share
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="Finance"
                      value={shareFilter}
                      onChange={(event) => handleSimpleFilterChange(setShareFilter, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Path Prefix
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="\\Finance\\Quarterly"
                      value={pathPrefix}
                      onChange={(event) => handleSimpleFilterChange(setPathPrefix, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Extension
                    <input
                      className={FILTER_INPUT_CLASS}
                      list="inventory-extension-options"
                      placeholder=".pst"
                      value={extFilter}
                      onChange={(event) => handleSimpleFilterChange(setExtFilter, event.target.value)}
                    />
                  </label>
                </div>
              ) : null}

              {activeTab === "resources" ? (
                <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  <label className={FILTER_LABEL_CLASS}>
                    Search
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="Share name or remark"
                      value={query}
                      onChange={(event) => handleSimpleFilterChange(setQuery, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Endpoint
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="host or ip"
                      value={endpointFilter}
                      onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Share Access
                    <select
                      className={FILTER_SELECT_CLASS}
                      value={resourceAccess}
                      onChange={(event) => handleSimpleFilterChange(setResourceAccess, event.target.value)}
                    >
                      <option value="">All</option>
                      <option value="readable">Readable</option>
                      <option value="list_only">List only</option>
                      <option value="no_access">No access</option>
                    </select>
                  </label>
                </div>
              ) : null}

              {activeTab === "endpoints" ? (
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <label className={FILTER_LABEL_CLASS}>
                    Endpoint Search
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="Hostname, endpoint key, or IP"
                      value={query}
                      onChange={(event) => handleSimpleFilterChange(setQuery, event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Endpoint Prefix
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="fs-"
                      value={endpointFilter}
                      onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)}
                    />
                  </label>
                </div>
              ) : null}

              {activeTab === "items" ? (
                <div className="mt-4 flex flex-wrap gap-2 text-xs">
                  {extensions.slice(0, 10).map((facet) => (
                    <button
                      className={`rounded-full border px-3 py-1.5 font-medium transition ${
                        extFilter === facet.ext
                          ? "border-emerald-600 bg-emerald-50 text-emerald-900 dark:bg-emerald-900/20 dark:text-emerald-100"
                          : "border-slate-300 text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                      }`}
                      key={facet.ext}
                      onClick={() => handleSimpleFilterChange(setExtFilter, extFilter === facet.ext ? "" : facet.ext)}
                      type="button"
                    >
                      {facet.ext} ({facet.count})
                    </button>
                  ))}
                </div>
              ) : null}
            </div>

            <div className="space-y-4">
              <div className="rounded-3xl border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-800 dark:bg-slate-900/40">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Run Scope</p>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                      {activeRunCount === 0 ? "Reviewing all runs." : `${activeRunCount} run${activeRunCount === 1 ? "" : "s"} selected.`}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                      onClick={() => setSelectedRunIds([])}
                      type="button"
                    >
                      Clear
                    </button>
                    <button
                      className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                      onClick={() => setShowRunScope((current) => !current)}
                      type="button"
                    >
                      {showRunScope ? "Hide" : "Choose Runs"}
                    </button>
                  </div>
                </div>

                {showRunScope ? (
                  <select
                    className="mt-3 h-36 w-full rounded-2xl border border-slate-300 bg-white p-3 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                    multiple
                    value={selectedRunIds}
                    onChange={(event) => {
                      const selected = Array.from(event.target.selectedOptions).map((option) => option.value);
                      setSelectedRunIds(selected);
                    }}
                  >
                    {runs.map((run) => (
                      <option key={run.id} value={run.id}>
                        {run.name} [{run.status}] {new Date(run.created_at).toLocaleString()}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>

              <div className="rounded-3xl border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-800 dark:bg-slate-900/40">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Advanced Query</p>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Use the DSL only when the guided filters are not enough.</p>
                  </div>
                  <button
                    className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    onClick={() => setShowAdvancedQuery((current) => !current)}
                    type="button"
                  >
                    {showAdvancedQuery ? "Hide" : "Open"}
                  </button>
                </div>

                {showAdvancedQuery ? (
                  <>
                    <textarea
                      className="mt-3 min-h-[96px] w-full rounded-2xl border border-slate-300 bg-white px-3 py-3 text-sm text-slate-900 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500"
                      placeholder={'endpoint startswith "fs-" AND share contains finance AND !ext = .tmp'}
                      value={inventoryQueryInput}
                      onChange={(event) => setInventoryQueryInput(event.target.value)}
                    />
                    <div className="mt-3 flex flex-wrap gap-2 text-xs">
                      <button
                        className="rounded-full border border-slate-300 px-3 py-1.5 transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        onClick={() => setInventoryQueryInput('share contains "finance" AND ext = .xlsx')}
                        type="button"
                      >
                        Finance spreadsheets
                      </button>
                      <button
                        className="rounded-full border border-slate-300 px-3 py-1.5 transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        onClick={() => setInventoryQueryInput('endpoint startswith "fs-" AND !ext = .tmp')}
                        type="button"
                      >
                        Server paths without temp files
                      </button>
                      <button
                        className="rounded-full border border-slate-300 px-3 py-1.5 transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        onClick={() => setInventoryQueryInput('access = readable OR access = list_only')}
                        type="button"
                      >
                        Reachable shares
                      </button>
                    </div>
                    <p className="mt-3 text-xs text-slate-500">
                      Fields: `search`, `endpoint`, `share`, `path`, `ext`, `access`. Operators: `=`, `~`, `^`, `AND`, `OR`, `!`.
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button
                        className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                        onClick={handleInventoryQueryApply}
                        type="button"
                      >
                        Apply Query
                      </button>
                      <button
                        className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                        onClick={() => {
                          setInventoryQueryInput("");
                          clearAppliedInventoryQuery();
                        }}
                        type="button"
                      >
                        Clear Query
                      </button>
                    </div>
                  </>
                ) : null}
              </div>

              <div className="rounded-3xl border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-800 dark:bg-slate-900/40">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Shared Investigations</p>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                      Save the current inventory view so other project members can reopen the same scope and filters.
                    </p>
                  </div>
                  <button
                    className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    onClick={() => {
                      setSelectedInvestigationId(null);
                      setInvestigationName("");
                      setInvestigationDescription("");
                    }}
                    type="button"
                  >
                    New
                  </button>
                </div>

                <div className="mt-3 grid gap-3">
                  <label className={FILTER_LABEL_CLASS}>
                    Investigation Name
                    <input
                      className={FILTER_INPUT_CLASS}
                      placeholder="Readable finance exposure"
                      value={investigationName}
                      onChange={(event) => setInvestigationName(event.target.value)}
                    />
                  </label>
                  <label className={FILTER_LABEL_CLASS}>
                    Notes
                    <textarea
                      className="mt-1 min-h-[84px] w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500"
                      placeholder="What makes this view useful for the team?"
                      value={investigationDescription}
                      onChange={(event) => setInvestigationDescription(event.target.value)}
                    />
                  </label>
                </div>

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                    onClick={saveInvestigation}
                    disabled={savingInvestigation}
                    type="button"
                  >
                    {savingInvestigation ? "Saving..." : "Save As New"}
                  </button>
                  <button
                    className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:hover:bg-slate-800"
                    onClick={updateInvestigation}
                    disabled={!selectedInvestigationId || savingInvestigation}
                    type="button"
                  >
                    Update Current
                  </button>
                </div>

                <div className="mt-4 space-y-3">
                  {savedInvestigations.length === 0 ? (
                    <p className="rounded-2xl border border-dashed border-slate-300 px-4 py-3 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
                      No shared investigations yet. Save the current view to create the first team-ready shortcut.
                    </p>
                  ) : (
                    savedInvestigations.map((investigation) => {
                      const isSelected = investigation.id === selectedInvestigationId;
                      return (
                        <div
                          key={investigation.id}
                          className={`rounded-2xl border px-4 py-3 ${
                            isSelected
                              ? "border-emerald-500 bg-emerald-50/80 dark:border-emerald-700 dark:bg-emerald-900/20"
                              : "border-slate-200 bg-white/80 dark:border-slate-800 dark:bg-slate-950/50"
                          }`}
                        >
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="text-sm font-semibold">{investigation.name}</p>
                                <span className="rounded-full border border-slate-300 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:border-slate-700">
                                  {INVENTORY_TAB_COPY[investigation.target_tab].label}
                                </span>
                                {isSelected ? (
                                  <span className="rounded-full border border-emerald-400 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-emerald-700 dark:border-emerald-700 dark:text-emerald-300">
                                    Selected
                                  </span>
                                ) : null}
                              </div>
                              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                Updated {new Date(investigation.updated_at).toLocaleString()}
                              </p>
                              {investigation.description ? (
                                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{investigation.description}</p>
                              ) : null}
                              {investigation.query_text ? (
                                <p className="mt-2 rounded-xl bg-slate-100 px-3 py-2 text-xs text-slate-600 dark:bg-slate-900 dark:text-slate-300">
                                  {investigation.query_text}
                                </p>
                              ) : null}
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <button
                                className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                                onClick={() => applySavedInvestigation(investigation)}
                                type="button"
                              >
                                Use
                              </button>
                              <button
                                className="rounded-xl border border-rose-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-rose-700 transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-60 dark:border-rose-800 dark:text-rose-200 dark:hover:bg-rose-900/20"
                                onClick={() => deleteInvestigation(investigation)}
                                disabled={deletingInvestigationId === investigation.id}
                                type="button"
                              >
                                {deletingInvestigationId === investigation.id ? "Deleting..." : "Delete"}
                              </button>
                            </div>
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>

        <datalist id="inventory-extension-options">
          {extensions.map((facet) => (
            <option key={facet.ext} value={facet.ext}>
              {facet.count}
            </option>
          ))}
        </datalist>
      </div>

      <div className="workspace-section overflow-x-auto">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">{activeTabCopy.label}</p>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              {activeResultCount.toLocaleString()} result{activeResultCount === 1 ? "" : "s"} on this page
            </p>
          </div>
          <div className="flex items-center gap-2">
            <details className="relative">
              <summary className="list-none cursor-pointer rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700">
                Columns
              </summary>
              <div className="absolute right-0 z-10 mt-1 w-72 rounded-2xl border border-slate-300 bg-white p-3 shadow-lg dark:border-slate-700 dark:bg-slate-900">
                <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">Drag to reorder visible columns</p>

                {activeTab === "items" ? (
                  <>
                    <div className="mb-2 space-y-1">
                      {itemColumns.map((column) => (
                        <div
                          key={`selected-${column}`}
                          className="flex cursor-grab items-center justify-between rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                          draggable
                          onDragStart={() => setDragItemColumn(column)}
                          onDragOver={(event) => event.preventDefault()}
                          onDrop={() => reorderItemColumns(column)}
                          onDragEnd={() => setDragItemColumn(null)}
                        >
                          <span>{ITEM_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>
                          <span className="text-slate-400">::</span>
                        </div>
                      ))}
                    </div>
                    <div className="max-h-44 overflow-auto border-t border-slate-200 pt-2 dark:border-slate-700">
                      {ITEM_COLUMN_OPTIONS.map((column) => (
                        <label className="mb-1 flex items-center gap-2 text-xs" key={column.key}>
                          <input
                            type="checkbox"
                            checked={itemColumns.includes(column.key)}
                            onChange={() => toggleItemColumn(column.key)}
                          />
                          <span>{column.label}</span>
                        </label>
                      ))}
                    </div>
                  </>
                ) : null}

                {activeTab === "resources" ? (
                  <>
                    <div className="mb-2 space-y-1">
                      {resourceColumns.map((column) => (
                        <div
                          key={`selected-${column}`}
                          className="flex cursor-grab items-center justify-between rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                          draggable
                          onDragStart={() => setDragResourceColumn(column)}
                          onDragOver={(event) => event.preventDefault()}
                          onDrop={() => reorderResourceColumns(column)}
                          onDragEnd={() => setDragResourceColumn(null)}
                        >
                          <span>{RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>
                          <span className="text-slate-400">::</span>
                        </div>
                      ))}
                    </div>
                    <div className="max-h-44 overflow-auto border-t border-slate-200 pt-2 dark:border-slate-700">
                      {RESOURCE_COLUMN_OPTIONS.map((column) => (
                        <label className="mb-1 flex items-center gap-2 text-xs" key={column.key}>
                          <input
                            type="checkbox"
                            checked={resourceColumns.includes(column.key)}
                            onChange={() => toggleResourceColumn(column.key)}
                          />
                          <span>{column.label}</span>
                        </label>
                      ))}
                    </div>
                  </>
                ) : null}

                {activeTab === "endpoints" ? (
                  <>
                    <div className="mb-2 space-y-1">
                      {endpointColumns.map((column) => (
                        <div
                          key={`selected-${column}`}
                          className="flex cursor-grab items-center justify-between rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                          draggable
                          onDragStart={() => setDragEndpointColumn(column)}
                          onDragOver={(event) => event.preventDefault()}
                          onDrop={() => reorderEndpointColumns(column)}
                          onDragEnd={() => setDragEndpointColumn(null)}
                        >
                          <span>{ENDPOINT_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</span>
                          <span className="text-slate-400">::</span>
                        </div>
                      ))}
                    </div>
                    <div className="max-h-44 overflow-auto border-t border-slate-200 pt-2 dark:border-slate-700">
                      {ENDPOINT_COLUMN_OPTIONS.map((column) => (
                        <label className="mb-1 flex items-center gap-2 text-xs" key={column.key}>
                          <input
                            type="checkbox"
                            checked={endpointColumns.includes(column.key)}
                            onChange={() => toggleEndpointColumn(column.key)}
                          />
                          <span>{column.label}</span>
                        </label>
                      ))}
                    </div>
                  </>
                ) : null}
              </div>
            </details>
            <button
              className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] disabled:opacity-50 dark:border-slate-700"
              disabled={cursorHistory.length === 0}
              onClick={movePrev}
              type="button"
            >
              Prev
            </button>
            <button
              className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] disabled:opacity-50 dark:border-slate-700"
              disabled={!nextCursor}
              onClick={moveNext}
              type="button"
            >
              Next
            </button>
          </div>
        </div>

        {activeResultCount === 0 ? (
          <div className="rounded-3xl border border-dashed border-slate-300 bg-slate-50/80 px-6 py-10 text-center dark:border-slate-700 dark:bg-slate-900/40">
            <p className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">{activeTabCopy.emptyTitle}</p>
            <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">{activeTabCopy.emptyBody}</p>
          </div>
        ) : null}

        {activeTab === "items" && activeResultCount > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                {itemColumns.map((column) => (
                  <th key={column}>{ITEM_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={row.id}>
                  {itemColumns.map((column) => (
                    <td key={`${row.id}-${column}`}>{itemCell(row, column)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}

        {activeTab === "resources" && activeResultCount > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                {resourceColumns.map((column) => (
                  <th key={column}>{RESOURCE_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {resources.map((row) => (
                <tr key={row.id}>
                  {resourceColumns.map((column) => (
                    <td key={`${row.id}-${column}`}>{resourceCell(row, column)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}

        {activeTab === "endpoints" && activeResultCount > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                {endpointColumns.map((column) => (
                  <th key={column}>{ENDPOINT_COLUMN_OPTIONS.find((entry) => entry.key === column)?.label || column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {endpoints.map((row) => (
                <tr key={row.id}>
                  {endpointColumns.map((column) => (
                    <td key={`${row.id}-${column}`}>{endpointCell(row, column)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </section>
  );
}
