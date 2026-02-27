"use client";

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type Endpoint = { id: number; endpoint_key: string; ip?: string; hostname?: string };
type Resource = { id: number; name: string; access_level: string };
type Item = { id: number; path: string; name: string; is_dir: boolean };

export default function RunDetailPage({ params }: { params: { projectId: string; runId: string } }) {
  const { projectId, runId } = params;

  const [endpointSearch, setEndpointSearch] = useState("");
  const [itemSearch, setItemSearch] = useState("");
  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [resources, setResources] = useState<Resource[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [selectedEndpoint, setSelectedEndpoint] = useState<number | null>(null);
  const [selectedResource, setSelectedResource] = useState<number | null>(null);

  useEffect(() => {
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints?search=${encodeURIComponent(endpointSearch)}&limit=100`).then((d) => {
      const list = d.items || [];
      setEndpoints(list);
      if (!selectedEndpoint && list.length) setSelectedEndpoint(list[0].id);
    });
  }, [projectId, runId, endpointSearch, selectedEndpoint]);

  useEffect(() => {
    if (!selectedEndpoint) return;
    apiFetch(`/projects/${projectId}/runs/${runId}/endpoints/${selectedEndpoint}/resources`).then((d) => {
      const list = d.items || [];
      setResources(list);
      if (list.length) setSelectedResource(list[0].id);
    });
  }, [projectId, runId, selectedEndpoint]);

  useEffect(() => {
    if (!selectedResource) return;
    apiFetch(
      `/projects/${projectId}/runs/${runId}/resources/${selectedResource}/items?search=${encodeURIComponent(itemSearch)}&limit=200`,
    ).then((d) => setItems(d.items || []));
  }, [projectId, runId, selectedResource, itemSearch]);

  return (
    <section className="space-y-6">
      <div className="panel">
        <h1 className="text-2xl font-bold">Run Explorer</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">Run: {runId}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="panel">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Endpoints</h2>
            <input
              className="w-40 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search"
              value={endpointSearch}
              onChange={(e) => setEndpointSearch(e.target.value)}
            />
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
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Items</h2>
            <input
              className="w-40 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search"
              value={itemSearch}
              onChange={(e) => setItemSearch(e.target.value)}
            />
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
    </section>
  );
}
