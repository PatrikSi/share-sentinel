import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type Project = { id: string; name: string };
type RunOption = { id: string; name: string; status: string; created_at: string };
type ExtensionFacet = { ext: string; count: number };
type ItemTypeFilter = "all" | "file" | "dir";
type SavedInvestigation = {
  id: string;
  project_id: string;
  created_by_user_id: string | null;
  name: string;
  description: string | null;
  target_tab: Tab;
  query_text: string;
  definition: InvestigationDefinition;
  created_at: string;
  updated_at: string;
};
type InvestigationDefinition = {
  active_tab: Tab;
  query: string;
  endpoint: string;
  share: string;
  path_prefix: string;
  ext: string;
  access_level: string;
  item_type: ItemTypeFilter;
  selected_run_ids: string[];
};

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

const DEFAULT_INVESTIGATION_DEFINITION: InvestigationDefinition = {
  active_tab: "items",
  query: "",
  endpoint: "",
  share: "",
  path_prefix: "",
  ext: "",
  access_level: "",
  item_type: "all",
  selected_run_ids: [],
};

function tokenizeInvestigationQuery(raw: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;

  for (const char of raw.trim()) {
    if (quote) {
      current += char;
      if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") {
      current += char;
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) {
        tokens.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }

  if (current) tokens.push(current);
  return tokens;
}

function stripQueryQuotes(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2 && ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'")))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function appendQuery(existing: string, nextValue: string): string {
  return [existing, nextValue].filter(Boolean).join(" ").trim();
}

function normalizeTabToken(value: string, fallback: Tab): Tab {
  const normalized = value.trim().toLowerCase();
  if (normalized === "items" || normalized === "files" || normalized === "folders") return "items";
  if (normalized === "resources" || normalized === "resource" || normalized === "shares" || normalized === "share") return "resources";
  if (normalized === "endpoints" || normalized === "hosts" || normalized === "endpoint") return "endpoints";
  return fallback;
}

function normalizeItemTypeToken(value: string): ItemTypeFilter {
  const normalized = value.trim().toLowerCase();
  if (normalized === "file" || normalized === "files") return "file";
  if (normalized === "dir" || normalized === "dirs" || normalized === "directory" || normalized === "directories" || normalized === "folder") return "dir";
  return "all";
}

function parseInvestigationQuery(raw: string, fallbackTab: Tab): InvestigationDefinition {
  const next: InvestigationDefinition = { ...DEFAULT_INVESTIGATION_DEFINITION, active_tab: fallbackTab, selected_run_ids: [] };

  for (const token of tokenizeInvestigationQuery(raw)) {
    const separatorIndex = token.indexOf(":");
    if (separatorIndex > 0) {
      const key = token.slice(0, separatorIndex).trim().toLowerCase();
      const value = stripQueryQuotes(token.slice(separatorIndex + 1));

      if (key === "tab" || key === "view") {
        next.active_tab = normalizeTabToken(value, next.active_tab);
        continue;
      }
      if (key === "query" || key === "q" || key === "text") {
        next.query = appendQuery(next.query, value);
        continue;
      }
      if (key === "endpoint" || key === "host" || key === "hostname" || key === "ip") {
        next.endpoint = value;
        continue;
      }
      if (key === "share") {
        next.share = value;
        continue;
      }
      if (key === "path") {
        next.path_prefix = value;
        continue;
      }
      if (key === "ext" || key === "extension") {
        next.ext = value;
        continue;
      }
      if (key === "access") {
        next.access_level = value;
        continue;
      }
      if (key === "type") {
        next.item_type = normalizeItemTypeToken(value);
        continue;
      }
      if (key === "run" || key === "runs") {
        next.selected_run_ids = value
          .split(",")
          .map((entry) => entry.trim())
          .filter(Boolean);
        continue;
      }
    }

    next.query = appendQuery(next.query, stripQueryQuotes(token));
  }

  return next;
}

function quoteInvestigationValue(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  return /\s/.test(trimmed) ? `"${trimmed}"` : trimmed;
}

function buildInvestigationQuery(definition: InvestigationDefinition): string {
  const tokens: string[] = [];
  if (definition.active_tab !== "items") tokens.push(`tab:${definition.active_tab}`);
  if (definition.query) tokens.push(`query:${quoteInvestigationValue(definition.query)}`);
  if (definition.endpoint) tokens.push(`endpoint:${quoteInvestigationValue(definition.endpoint)}`);
  if (definition.share) tokens.push(`share:${quoteInvestigationValue(definition.share)}`);
  if (definition.path_prefix) tokens.push(`path:${quoteInvestigationValue(definition.path_prefix)}`);
  if (definition.ext) tokens.push(`ext:${quoteInvestigationValue(definition.ext)}`);
  if (definition.access_level) tokens.push(`access:${quoteInvestigationValue(definition.access_level)}`);
  if (definition.item_type !== "all") tokens.push(`type:${definition.item_type}`);
  if (definition.selected_run_ids.length > 0) tokens.push(`runs:${definition.selected_run_ids.join(",")}`);
  return tokens.join(" ");
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
  const [itemTypeFilter, setItemTypeFilter] = useState<ItemTypeFilter>("all");
  const [investigationQuery, setInvestigationQuery] = useState("");
  const [investigationName, setInvestigationName] = useState("");
  const [investigationDescription, setInvestigationDescription] = useState("");
  const [savedInvestigations, setSavedInvestigations] = useState<SavedInvestigation[]>([]);
  const [investigationsLoading, setInvestigationsLoading] = useState(false);

  const [extensions, setExtensions] = useState<ExtensionFacet[]>([]);
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [resources, setResources] = useState<InventoryResource[]>([]);
  const [endpoints, setEndpoints] = useState<InventoryEndpoint[]>([]);

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
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

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}`)
      .then((data) => setProject(data as Project))
      .catch((err) => setError(err.message));

    apiFetch(`/projects/${projectId}/runs?limit=200`)
      .then((data) => setRuns((data?.items || []) as RunOption[]))
      .catch((err) => setError(err.message));
  }, [projectId]);

  async function loadInvestigations() {
    if (!projectId) return;
    setInvestigationsLoading(true);
    try {
      const data = await apiFetch(`/projects/${projectId}/inventory/investigations`);
      setSavedInvestigations((data?.items || []) as SavedInvestigation[]);
    } finally {
      setInvestigationsLoading(false);
    }
  }

  useEffect(() => {
    loadInvestigations().catch((err) => setError(err instanceof Error ? err.message : "Failed to load saved investigations"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  }, [activeTab, projectId, runIdsParam, query, endpointFilter, shareFilter, pathPrefix, extFilter, resourceAccess, itemTypeFilter]);

  useEffect(() => {
    if (!projectId) return;
    const queryParams = new URLSearchParams({ limit: "200" });
    if (cursor) queryParams.set("cursor", cursor);
    if (runIdsParam) queryParams.set("run_ids", runIdsParam);

    if (activeTab === "items") {
      if (query.trim()) queryParams.set("q", query.trim());
      if (endpointFilter.trim()) queryParams.set("endpoint", endpointFilter.trim());
      if (shareFilter.trim()) queryParams.set("share", shareFilter.trim());
      if (pathPrefix.trim()) queryParams.set("path_prefix", pathPrefix.trim());
      if (extFilter.trim()) queryParams.set("ext", extFilter.trim());
      if (itemTypeFilter === "file") queryParams.set("is_dir", "false");
      if (itemTypeFilter === "dir") queryParams.set("is_dir", "true");

      apiFetch(`/projects/${projectId}/inventory/items?${queryParams.toString()}`)
        .then((data) => {
          setItems((data?.items || []) as InventoryItem[]);
          setNextCursor((data?.next_cursor as string | null) || null);
        })
        .catch((err) => setError(err.message));
      return;
    }

    if (activeTab === "resources") {
      if (query.trim()) queryParams.set("q", query.trim());
      if (endpointFilter.trim()) queryParams.set("endpoint", endpointFilter.trim());
      if (resourceAccess.trim()) queryParams.set("access_level", resourceAccess.trim());

      apiFetch(`/projects/${projectId}/inventory/resources?${queryParams.toString()}`)
        .then((data) => {
          setResources((data?.items || []) as InventoryResource[]);
          setNextCursor((data?.next_cursor as string | null) || null);
        })
        .catch((err) => setError(err.message));
      return;
    }

    if (endpointQuery) queryParams.set("q", endpointQuery);
    apiFetch(`/projects/${projectId}/inventory/endpoints?${queryParams.toString()}`)
      .then((data) => {
        setEndpoints((data?.items || []) as InventoryEndpoint[]);
        setNextCursor((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [activeTab, cursor, endpointFilter, endpointQuery, extFilter, itemTypeFilter, pathPrefix, projectId, query, resourceAccess, runIdsParam, shareFilter]);

  function currentInvestigationDefinition(): InvestigationDefinition {
    return {
      active_tab: activeTab,
      query: query.trim(),
      endpoint: endpointFilter.trim(),
      share: shareFilter.trim(),
      path_prefix: pathPrefix.trim(),
      ext: extFilter.trim(),
      access_level: resourceAccess.trim(),
      item_type: itemTypeFilter,
      selected_run_ids: selectedRunIds,
    };
  }

  function applyInvestigationDefinition(definition: InvestigationDefinition, queryText?: string, successMessage?: string) {
    const normalized = { ...DEFAULT_INVESTIGATION_DEFINITION, ...definition };
    setActiveTab(normalized.active_tab || "items");
    setQuery(normalized.query || "");
    setEndpointFilter(normalized.endpoint || "");
    setShareFilter(normalized.share || "");
    setPathPrefix(normalized.path_prefix || "");
    setExtFilter(normalized.ext || "");
    setResourceAccess(normalized.access_level || "");
    setItemTypeFilter(normalized.item_type || "all");
    setSelectedRunIds(Array.isArray(normalized.selected_run_ids) ? normalized.selected_run_ids : []);
    setInvestigationQuery(queryText || buildInvestigationQuery(normalized));
    setError(null);
    if (successMessage) setInfo(successMessage);
  }

  function applyInvestigationQuery() {
    const parsed = parseInvestigationQuery(investigationQuery, activeTab);
    applyInvestigationDefinition(parsed, investigationQuery, "Applied investigation query.");
  }

  function captureCurrentFilters() {
    const definition = currentInvestigationDefinition();
    setInvestigationQuery(buildInvestigationQuery(definition));
    setError(null);
    setInfo("Captured the current filters into the investigation query.");
  }

  async function saveInvestigation() {
    if (!projectId) return;
    if (!investigationName.trim()) {
      setError("Investigation name is required.");
      return;
    }

    const definition = investigationQuery.trim() ? parseInvestigationQuery(investigationQuery, activeTab) : currentInvestigationDefinition();
    const queryText = investigationQuery.trim() || buildInvestigationQuery(definition);

    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/projects/${projectId}/inventory/investigations`, {
        method: "POST",
        body: JSON.stringify({
          name: investigationName.trim(),
          description: investigationDescription.trim() || null,
          target_tab: definition.active_tab,
          query_text: queryText,
          definition,
        }),
      });
      setInvestigationName("");
      setInvestigationDescription("");
      setInfo("Saved investigation.");
      await loadInvestigations();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save investigation");
    }
  }

  async function deleteInvestigation(investigation: SavedInvestigation) {
    if (!projectId) return;
    if (!window.confirm(`Delete saved investigation "${investigation.name}"?`)) return;

    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/projects/${projectId}/inventory/investigations/${investigation.id}`, { method: "DELETE" });
      setInfo(`Deleted "${investigation.name}".`);
      await loadInvestigations();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete investigation");
    }
  }

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
        {info ? <p className="rounded-lg bg-emerald-100 p-2 text-sm text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-200">{info}</p> : null}
      </div>

      <div className="workspace-section">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.7fr)_minmax(320px,0.9fr)]">
          <div className="workspace-card space-y-4">
            <div>
              <h2 className="text-lg font-semibold">Investigation Query</h2>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Use a simple `field:value` query to repeat audit workflows without resetting each filter manually.
              </p>
              <p className="mt-2 text-xs text-slate-500">
                Supported keys: `tab`, `query`, `endpoint`, `share`, `path`, `ext`, `access`, `type`, `runs`.
              </p>
            </div>

            <textarea
              className="min-h-[96px] w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder={'query:"finance review" endpoint:fs-01 ext:.xlsx type:file'}
              value={investigationQuery}
              onChange={(event) => setInvestigationQuery(event.target.value)}
            />

            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700"
                onClick={applyInvestigationQuery}
                type="button"
              >
                Apply Query
              </button>
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700"
                onClick={captureCurrentFilters}
                type="button"
              >
                Capture Current Filters
              </button>
            </div>

            <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Save As
                <input
                  className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  placeholder="Quarterly finance workbook review"
                  value={investigationName}
                  onChange={(event) => setInvestigationName(event.target.value)}
                />
              </label>
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Notes
                <input
                  className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  placeholder="Optional analyst note"
                  value={investigationDescription}
                  onChange={(event) => setInvestigationDescription(event.target.value)}
                />
              </label>
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase dark:border-slate-700"
                onClick={saveInvestigation}
                type="button"
              >
                Save Investigation
              </button>
            </div>
          </div>

          <div className="workspace-card">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h2 className="text-lg font-semibold">Saved Investigations</h2>
                <p className="mt-1 text-xs text-slate-500">Persisted per project for repeat audit work.</p>
              </div>
              {investigationsLoading ? <span className="text-xs text-slate-500">Loading…</span> : null}
            </div>

            <ul className="mt-3 max-h-[320px] space-y-2 overflow-auto">
              {savedInvestigations.length === 0 ? <li className="text-sm text-slate-500">No saved investigations yet.</li> : null}
              {savedInvestigations.map((investigation) => (
                <li className="rounded-lg border border-slate-300 p-3 dark:border-slate-700" key={investigation.id}>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="font-semibold">{investigation.name}</p>
                      <p className="mt-1 text-[11px] uppercase tracking-wide text-slate-500">{investigation.target_tab}</p>
                    </div>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-[10px] font-semibold uppercase dark:border-slate-700"
                      onClick={() => deleteInvestigation(investigation)}
                      type="button"
                    >
                      Delete
                    </button>
                  </div>
                  {investigation.description ? <p className="mt-2 text-xs text-slate-500">{investigation.description}</p> : null}
                  <div className="mt-2 rounded bg-slate-100 px-2 py-2 text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                    {investigation.query_text || buildInvestigationQuery(investigation.definition)}
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-2 text-[11px] text-slate-500">
                    <span>Updated {new Date(investigation.updated_at).toLocaleString()}</span>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 font-semibold uppercase dark:border-slate-700"
                      onClick={() =>
                        applyInvestigationDefinition(
                          investigation.definition || DEFAULT_INVESTIGATION_DEFINITION,
                          investigation.query_text,
                          `Applied "${investigation.name}".`,
                        )
                      }
                      type="button"
                    >
                      Apply
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>

      <div className="workspace-section">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-7">
          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Search
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="File, path, hostname, share"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Endpoint
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="host or ip"
              value={endpointFilter}
              onChange={(event) => setEndpointFilter(event.target.value)}
            />
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Share
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="share name"
              value={shareFilter}
              onChange={(event) => setShareFilter(event.target.value)}
              disabled={activeTab !== "items"}
            />
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Path Prefix
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="\\HR\\"
              value={pathPrefix}
              onChange={(event) => setPathPrefix(event.target.value)}
              disabled={activeTab !== "items"}
            />
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Extension
            <select
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={extFilter}
              onChange={(event) => setExtFilter(event.target.value)}
              disabled={activeTab !== "items"}
            >
              <option value="">All</option>
              {extensions.map((facet) => (
                <option key={facet.ext} value={facet.ext}>
                  {facet.ext} ({facet.count})
                </option>
              ))}
            </select>
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Share Access
            <select
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={resourceAccess}
              onChange={(event) => setResourceAccess(event.target.value)}
              disabled={activeTab !== "resources"}
            >
              <option value="">All</option>
              <option value="readable">readable</option>
              <option value="list_only">list_only</option>
              <option value="no_access">no_access</option>
            </select>
          </label>

          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Entry Type
            <select
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={itemTypeFilter}
              onChange={(event) => setItemTypeFilter(event.target.value as ItemTypeFilter)}
              disabled={activeTab !== "items"}
            >
              <option value="all">All</option>
              <option value="file">Files</option>
              <option value="dir">Directories</option>
            </select>
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
                onClick={() => setExtFilter((prev) => (prev === facet.ext ? "" : facet.ext))}
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
