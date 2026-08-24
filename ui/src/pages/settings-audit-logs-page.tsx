import { FormEvent, useEffect, useState } from "react";

import { Dialog } from "@/components/dialog";
import { StatePanel } from "@/components/state-panel";
import { apiFetch, apiFetchBlob } from "@/lib/api";

type AuditEventRow = {
  id: number;
  ts: string;
  actor_user_id: string | null;
  actor_email: string | null;
  actor_token_id: string | null;
  project_id: string | null;
  project_name: string | null;
  action: string;
  object_type: string;
  object_id: string;
  metadata: Record<string, unknown>;
};

type ProjectOption = {
  id: string;
  name: string;
};

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function SettingsAuditLogsPage() {
  const [events, setEvents] = useState<AuditEventRow[]>([]);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [selectedEvent, setSelectedEvent] = useState<AuditEventRow | null>(null);

  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [projectFilterDraft, setProjectFilterDraft] = useState("all");
  const [projectFilter, setProjectFilter] = useState("all");
  const [loading, setLoading] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);

  async function loadProjects() {
    const data = await apiFetch("/settings/projects");
    setProjects((data || []) as ProjectOption[]);
  }

  async function loadAuditLogs() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
      if (projectFilter !== "all") query.set("project_id", projectFilter);
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/audit?${query.toString()}`);
      setEvents((data?.items || []) as AuditEventRow[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadProjects().catch((err) => setError(err instanceof Error ? err.message : "Failed to load projects"));
  }, []);

  useEffect(() => {
    loadAuditLogs().catch((err) => setError(err instanceof Error ? err.message : "Failed to load audit logs"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, projectFilter, search]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursor(null);
    setHistory([]);
    setSearch(searchDraft.trim());
    setProjectFilter(projectFilterDraft);
  }

  function clearFilters() {
    setCursor(null);
    setHistory([]);
    setSearchDraft("");
    setSearch("");
    setProjectFilterDraft("all");
    setProjectFilter("all");
  }

  function previousPage() {
    if (history.length === 0) return;
    const copy = [...history];
    const previous = copy.pop() ?? null;
    setHistory(copy);
    setCursor(previous);
  }

  function nextPage() {
    if (!nextCursor) return;
    setHistory((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }

  async function exportAuditLogs(format: "csv" | "json") {
    setError(null);
    setExporting(format);
    try {
      const query = new URLSearchParams({ format, max_rows: "5000" });
      if (search.trim()) query.set("q", search.trim());
      if (projectFilter !== "all") query.set("project_id", projectFilter);
      const { blob, filename } = await apiFetchBlob(`/settings/audit/export?${query.toString()}`);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || `share-sentinel-audit.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to export audit logs");
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <h2 className="settings-page-title">Audit Log</h2>
          <p className="settings-page-copy">Review privileged activity with simple filters and explicit export scope.</p>
        </div>
        <div className="settings-toolbar">
          <button className="settings-button" disabled={exporting !== null} onClick={() => exportAuditLogs("csv")} type="button">
            {exporting === "csv" ? "Exporting CSV..." : "Export CSV"}
          </button>
          <button className="settings-button" disabled={exporting !== null} onClick={() => exportAuditLogs("json")} type="button">
            {exporting === "json" ? "Exporting JSON..." : "Export JSON"}
          </button>
        </div>
      </div>

      {error ? (
        <div className="settings-panel">
          <p className="text-sm text-rose-700 dark:text-rose-200">{error}</p>
        </div>
      ) : null}

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Filters</h3>
            <p className="settings-panel-copy">Exports honor the current search and project filter, up to 5,000 rows.</p>
          </div>
        </div>

        <form className="mt-4 grid gap-4" onSubmit={applyFilters}>
          <div className="settings-grid-2">
            <label className="settings-field">
              <span className="settings-label">Search</span>
              <input
                className="settings-input"
                placeholder="Action, object, actor, or project"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
              />
            </label>

            <label className="settings-field">
              <span className="settings-label">Project</span>
              <select className="settings-select" value={projectFilterDraft} onChange={(event) => setProjectFilterDraft(event.target.value)}>
                <option value="all">All projects</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="settings-toolbar">
            <button className="settings-button-primary" type="submit">
              Apply Filters
            </button>
            <button className="settings-button" onClick={clearFilters} type="button">
              Clear
            </button>
          </div>
        </form>
      </section>

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Events</h3>
            <p className="settings-panel-copy">Full metadata is available per event instead of inline in every row.</p>
          </div>
        </div>

        {loading ? (
          <div className="mt-4">
            <StatePanel title="Loading Audit Events" description="Fetching audit activity." />
          </div>
        ) : events.length === 0 ? (
          <div className="mt-4 settings-empty">No audit events matched the current filters.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <caption className="sr-only">Audit events matching the current filters</caption>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Actor</th>
                  <th>Scope</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.id}>
                    <td>{formatTime(event.ts)}</td>
                    <td>{event.action}</td>
                    <td>
                      <div>{event.object_type}</div>
                      <div className="settings-meta">{event.object_id}</div>
                    </td>
                    <td>{event.actor_email || event.actor_user_id || "system"}</td>
                    <td>{event.project_name || event.project_id || "Global"}</td>
                    <td className="text-right">
                      <button className="settings-button" onClick={() => setSelectedEvent(event)} type="button">
                        Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4 settings-toolbar">
          <button className="settings-button" disabled={history.length === 0} onClick={previousPage} type="button">
            Previous
          </button>
          <button className="settings-button" disabled={!nextCursor} onClick={nextPage} type="button">
            Next
          </button>
        </div>
      </section>

      <Dialog
        open={selectedEvent !== null}
        onClose={() => setSelectedEvent(null)}
        title={selectedEvent ? `${selectedEvent.action} details` : "Event details"}
        description={selectedEvent ? `${selectedEvent.object_type}: ${selectedEvent.object_id}` : ""}
        footer={
          <button className="settings-button" onClick={() => setSelectedEvent(null)} type="button">
            Close
          </button>
        }
      >
        {selectedEvent ? (
          <div className="grid gap-4 text-sm">
            <div className="grid gap-2">
              <div className="flex items-start justify-between gap-3">
                <span className="text-slate-500 dark:text-slate-400">Time</span>
                <span>{formatTime(selectedEvent.ts)}</span>
              </div>
              <div className="flex items-start justify-between gap-3">
                <span className="text-slate-500 dark:text-slate-400">Actor</span>
                <span>{selectedEvent.actor_email || selectedEvent.actor_user_id || "system"}</span>
              </div>
              <div className="flex items-start justify-between gap-3">
                <span className="text-slate-500 dark:text-slate-400">Project</span>
                <span>{selectedEvent.project_name || selectedEvent.project_id || "Global"}</span>
              </div>
            </div>
            <div className="settings-field">
              <span className="settings-label">Metadata</span>
              <pre className="max-h-[320px] overflow-auto rounded-md border p-3 text-xs" style={{ borderColor: "var(--app-border)" }}>
                {JSON.stringify(selectedEvent.metadata || {}, null, 2)}
              </pre>
            </div>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
