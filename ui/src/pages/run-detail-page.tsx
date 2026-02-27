import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type Endpoint = { id: number; endpoint_key: string };
type Resource = { id: number; name: string; access_level: string };
type Item = { id: number; path: string; is_dir: boolean; resource_id?: number; name?: string };

export function RunDetailPage() {
  const { projectId, runId } = useParams<{ projectId: string; runId: string }>();

  const [error, setError] = useState<string | null>(null);

  const [endpointSearch, setEndpointSearch] = useState("");
  const [itemSearch, setItemSearch] = useState("");
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

  useEffect(() => {
    setEndpointCursor(null);
    setEndpointHistory([]);
  }, [endpointSearch, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "100", search: endpointSearch });
    if (endpointCursor) query.set("cursor", endpointCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints?${query.toString()}`)
      .then((data) => {
        const rows = (data?.items || []) as Endpoint[];
        setEndpoints(rows);
        setEndpointNext((data?.next_cursor as string | null) || null);
        if (!selectedEndpoint && rows.length > 0) {
          setSelectedEndpoint(rows[0].id);
        }
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, endpointSearch, endpointCursor, selectedEndpoint]);

  useEffect(() => {
    if (!projectId || !runId || !selectedEndpoint) return;
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources`)
      .then((data) => {
        const rows = (data?.items || []) as Resource[];
        setResources(rows);
        setSelectedResource(rows.length > 0 ? rows[0].id : null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, selectedEndpoint]);

  useEffect(() => {
    setItemCursor(null);
    setItemHistory([]);
  }, [selectedResource, itemSearch, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId || !selectedResource) return;
    const query = new URLSearchParams({ limit: "200", search: itemSearch });
    if (itemCursor) query.set("cursor", itemCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/resources/${selectedResource}/items?${query.toString()}`)
      .then((data) => {
        setItems((data?.items || []) as Item[]);
        setItemNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, selectedResource, itemSearch, itemCursor]);

  useEffect(() => {
    setGlobalCursor(null);
    setGlobalHistory([]);
  }, [globalQuery, globalExt, projectId, runId]);

  useEffect(() => {
    if (!projectId || !runId) return;
    const query = new URLSearchParams({ limit: "200", q: globalQuery });
    if (globalExt) query.set("ext", globalExt);
    if (globalCursor) query.set("cursor", globalCursor);

    apiFetch(`/projects/${projectId}/runs/${runId}/search/items?${query.toString()}`)
      .then((data) => {
        setGlobalItems((data?.items || []) as Item[]);
        setGlobalNext((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [projectId, runId, globalQuery, globalExt, globalCursor]);

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

  return (
    <section className="space-y-6">
      <div className="panel">
        <h1 className="text-2xl font-bold">Run Explorer</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">Run: {runId}</p>
        {error ? <p className="mt-2 text-sm text-red-600">{error}</p> : null}
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="panel">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Endpoints</h2>
            <input
              className="w-40 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search"
              value={endpointSearch}
              onChange={(event) => setEndpointSearch(event.target.value)}
            />
          </div>
          <div className="mb-2 flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveBack(setEndpointCursor, setEndpointHistory)}
              disabled={endpointHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveCursor(endpointNext, endpointCursor, setEndpointCursor, setEndpointHistory)}
              disabled={!endpointNext}
            >
              Next
            </button>
          </div>
          <ul className="space-y-2">
            {endpoints.map((endpoint) => (
              <li key={endpoint.id}>
                <button
                  className={`w-full rounded-lg border px-2 py-2 text-left text-xs ${
                    selectedEndpoint === endpoint.id
                      ? "border-ember bg-ember/10"
                      : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  }`}
                  onClick={() => setSelectedEndpoint(endpoint.id)}
                >
                  {endpoint.endpoint_key}
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <h2 className="mb-3 text-lg font-semibold">Shares</h2>
          <ul className="space-y-2">
            {resources.map((resource) => (
              <li key={resource.id}>
                <button
                  className={`w-full rounded-lg border px-2 py-2 text-left text-xs ${
                    selectedResource === resource.id
                      ? "border-pine bg-pine/10"
                      : "border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                  }`}
                  onClick={() => setSelectedResource(resource.id)}
                >
                  <span className="block font-semibold">{resource.name}</span>
                  <span className="text-slate-500">{resource.access_level}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Items</h2>
            <input
              className="w-40 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search"
              value={itemSearch}
              onChange={(event) => setItemSearch(event.target.value)}
            />
          </div>
          <div className="mb-2 flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveBack(setItemCursor, setItemHistory)}
              disabled={itemHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={() => moveCursor(itemNext, itemCursor, setItemCursor, setItemHistory)}
              disabled={!itemNext}
            >
              Next
            </button>
          </div>
          <ul className="max-h-[420px] space-y-2 overflow-auto">
            {items.map((item) => (
              <li key={item.id} className="rounded-lg border border-slate-300 px-2 py-2 text-xs dark:border-slate-700">
                <div className="font-mono">{item.path}</div>
                <div className="text-slate-500">{item.is_dir ? "directory" : "file"}</div>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="panel">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Run-Scoped Search</h2>
          <div className="flex items-center gap-2">
            <input
              className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Query"
              value={globalQuery}
              onChange={(event) => setGlobalQuery(event.target.value)}
            />
            <input
              className="w-20 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder=".ext"
              value={globalExt}
              onChange={(event) => setGlobalExt(event.target.value)}
            />
          </div>
        </div>

        <div className="mb-2 flex items-center gap-2">
          <button
            className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
            onClick={() => moveBack(setGlobalCursor, setGlobalHistory)}
            disabled={globalHistory.length === 0}
          >
            Prev
          </button>
          <button
            className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
            onClick={() => moveCursor(globalNext, globalCursor, setGlobalCursor, setGlobalHistory)}
            disabled={!globalNext}
          >
            Next
          </button>
        </div>

        <ul className="max-h-[280px] space-y-2 overflow-auto">
          {globalItems.map((item) => (
            <li key={item.id} className="rounded-lg border border-slate-300 px-2 py-2 text-xs dark:border-slate-700">
              <div className="font-mono">{item.path}</div>
              <div className="text-slate-500">resource_id: {item.resource_id ?? "-"}</div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
