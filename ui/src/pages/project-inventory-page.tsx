import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiFetch } from "@/lib/api";
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

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}`)
      .then((data) => setProject(data as Project))
      .catch((err) => setError(err.message));

    apiFetch(`/projects/${projectId}/runs?limit=200`)
      .then((data) => setRuns((data?.items || []) as RunOption[]))
      .catch((err) => setError(err.message));
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

  return (
    <section className="workspace">
      <div className="workspace-header gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">Project Inventory</h1>
            <p className="text-sm text-slate-600 dark:text-slate-300">
              {project ? `${project.name} (${project.id})` : projectId}
            </p>
          </div>
        <div className="flex items-center gap-2">
          <Link className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700" to="/projects">
            Back to Projects
          </Link>
        </div>
      </div>
        {error ? <p className="rounded-lg bg-rose-100 p-2 text-sm text-rose-700 dark:bg-rose-900/20 dark:text-rose-200">{error}</p> : null}
        {queryError ? <p className="rounded-lg bg-amber-100 p-2 text-sm text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">{queryError}</p> : null}
      </div>

      <div className="workspace-section">
        <div className="rounded-2xl border border-slate-300 bg-slate-50/70 p-4 dark:border-slate-700 dark:bg-slate-900/40">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Inventory Query</p>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Use `field operator value` clauses with `AND`, `OR`, `NOT`, or `!`. Supported fields: `search`, `endpoint`, `share`, `path`, `ext`, `access`.
              </p>
              <p className="mt-1 text-xs text-slate-500">
                Operators: `equals` or `=`, `contains` or `~`, `startswith` or `^`. AND has higher precedence than OR. Use quotes for spaces.
              </p>
            </div>
            {queryModeActive ? (
              <span className="rounded-full bg-emerald-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200">
                Query mode active
              </span>
            ) : null}
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto] lg:items-start">
            <textarea
              className="min-h-[86px] w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder={'endpoint startswith "fs-" AND share contains finance AND !ext = .tmp'}
              value={inventoryQueryInput}
              onChange={(event) => setInventoryQueryInput(event.target.value)}
            />
            <button
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700"
              onClick={handleInventoryQueryApply}
              type="button"
            >
              Apply Query
            </button>
            <button
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700"
              onClick={() => {
                setInventoryQueryInput("");
                clearAppliedInventoryQuery();
              }}
              type="button"
            >
              Clear Query
            </button>
          </div>

          {queryModeActive ? (
            <p className="mt-3 text-xs text-slate-500">
              The query is the source of truth while active. The filters below show the extracted values; editing a filter switches back to simple filter mode.
            </p>
          ) : null}
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-6">
          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Search</span>
              {queryModeActive && queryFilterReflections.search.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.search.modeLabel}</span>
              ) : null}
            </span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="File, path, hostname, share"
              value={query}
              onChange={(event) => handleSimpleFilterChange(setQuery, event.target.value)}
            />
            {queryModeActive && queryFilterReflections.search.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.search.summary}</p>
            ) : null}
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Endpoint</span>
              {queryModeActive && queryFilterReflections.endpoint.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.endpoint.modeLabel}</span>
              ) : null}
            </span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="host or ip"
              value={endpointFilter}
              onChange={(event) => handleSimpleFilterChange(setEndpointFilter, event.target.value)}
            />
            {queryModeActive && queryFilterReflections.endpoint.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.endpoint.summary}</p>
            ) : null}
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Share</span>
              {queryModeActive && queryFilterReflections.share.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.share.modeLabel}</span>
              ) : null}
            </span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="share name"
              value={shareFilter}
              onChange={(event) => handleSimpleFilterChange(setShareFilter, event.target.value)}
              disabled={activeTab !== "items"}
            />
            {queryModeActive && queryFilterReflections.share.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.share.summary}</p>
            ) : null}
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Path Prefix</span>
              {queryModeActive && queryFilterReflections.path.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.path.modeLabel}</span>
              ) : null}
            </span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="\\HR\\"
              value={pathPrefix}
              onChange={(event) => handleSimpleFilterChange(setPathPrefix, event.target.value)}
              disabled={activeTab !== "items"}
            />
            {queryModeActive && queryFilterReflections.path.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.path.summary}</p>
            ) : null}
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Extension</span>
              {queryModeActive && queryFilterReflections.ext.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.ext.modeLabel}</span>
              ) : null}
            </span>
            <select
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={extFilter}
              onChange={(event) => handleSimpleFilterChange(setExtFilter, event.target.value)}
              disabled={activeTab !== "items"}
            >
              <option value="">All</option>
              {extensions.map((facet) => (
                <option key={facet.ext} value={facet.ext}>
                  {facet.ext} ({facet.count})
                </option>
              ))}
            </select>
            {queryModeActive && queryFilterReflections.ext.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.ext.summary}</p>
            ) : null}
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            <span className="flex flex-wrap items-center gap-2">
              <span>Share Access</span>
              {queryModeActive && queryFilterReflections.access.modeLabel ? (
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] dark:bg-slate-800">{queryFilterReflections.access.modeLabel}</span>
              ) : null}
            </span>
            <select
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={resourceAccess}
              onChange={(event) => handleSimpleFilterChange(setResourceAccess, event.target.value)}
              disabled={activeTab !== "resources"}
            >
              <option value="">All</option>
              <option value="readable">readable</option>
              <option value="list_only">list_only</option>
              <option value="no_access">no_access</option>
            </select>
            {queryModeActive && queryFilterReflections.access.summary ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal text-slate-500">{queryFilterReflections.access.summary}</p>
            ) : null}
          </label>
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-[3fr_2fr]">
          <div className="flex flex-wrap gap-2">
            <button
              className={`rounded-lg border px-3 py-1 text-xs font-semibold uppercase ${
                activeTab === "items" ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20" : "border-slate-300 dark:border-slate-700"
              }`}
              onClick={() => setActiveTab("items")}
            >
              Files & Folders
            </button>
            <button
              className={`rounded-lg border px-3 py-1 text-xs font-semibold uppercase ${
                activeTab === "resources" ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20" : "border-slate-300 dark:border-slate-700"
              }`}
              onClick={() => setActiveTab("resources")}
            >
              Shares
            </button>
            <button
              className={`rounded-lg border px-3 py-1 text-xs font-semibold uppercase ${
                activeTab === "endpoints" ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20" : "border-slate-300 dark:border-slate-700"
              }`}
              onClick={() => setActiveTab("endpoints")}
            >
              Endpoints
            </button>
          </div>

          <div className="flex items-center justify-end gap-2">
            <details className="relative">
              <summary className="list-none cursor-pointer rounded border border-slate-300 px-3 py-1 text-xs font-semibold uppercase dark:border-slate-700">
                Columns
              </summary>
              <div className="absolute right-0 z-10 mt-1 w-72 rounded-lg border border-slate-300 bg-white p-2 shadow-lg dark:border-slate-700 dark:bg-slate-900">
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
              className="rounded border border-slate-300 px-3 py-1 text-xs font-semibold uppercase disabled:opacity-50 dark:border-slate-700"
              disabled={cursorHistory.length === 0}
              onClick={movePrev}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-3 py-1 text-xs font-semibold uppercase disabled:opacity-50 dark:border-slate-700"
              disabled={!nextCursor}
              onClick={moveNext}
            >
              Next
            </button>
          </div>
        </div>

        <div className="mt-3">
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-slate-500">Run Filter (multi-select)</label>
          <select
            className="h-28 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm dark:border-slate-700 dark:bg-slate-900"
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
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <button className="rounded border border-slate-300 px-2 py-1 dark:border-slate-700" onClick={() => setSelectedRunIds([])}>
              Clear run filter
            </button>
            {extensions.slice(0, 12).map((facet) => (
              <button
                className={`rounded-full border px-2 py-1 ${extFilter === facet.ext ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-900/20" : "border-slate-300 dark:border-slate-700"}`}
                key={facet.ext}
                onClick={() => handleSimpleFilterChange(setExtFilter, extFilter === facet.ext ? "" : facet.ext)}
              >
                {facet.ext} ({facet.count})
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="workspace-section overflow-x-auto">
        {activeTab === "items" ? (
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

        {activeTab === "resources" ? (
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

        {activeTab === "endpoints" ? (
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
