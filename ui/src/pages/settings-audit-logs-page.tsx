import { FormEvent, useEffect, useState } from "react";

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

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function SettingsAuditLogsPage() {
  const [events, setEvents] = useState<AuditEventRow[]>([]);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<"csv" | "json" | null>(null);

  async function loadAuditLogs() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/audit?${query.toString()}`);
      setEvents((data?.items || []) as AuditEventRow[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAuditLogs().catch((err) => setError(err instanceof Error ? err.message : "Failed to load audit logs"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  function onSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursor(null);
    setHistory([]);
    setSearch(searchDraft.trim());
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
    <>
      {error ? (
        <div className="workspace-section space-y-2">
          <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p>
        </div>
      ) : null}

      <div className="workspace-section space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Global Audit Logs</h2>
          <div className="flex flex-wrap items-center gap-2">
            <form className="flex items-center gap-2" onSubmit={onSearch}>
              <input
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search action, object, actor, project"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
              />
              <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" type="submit">
                Search
              </button>
              <button
                className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                type="button"
                onClick={() => {
                  setSearchDraft("");
                  setSearch("");
                  setCursor(null);
                  setHistory([]);
                }}
              >
                Clear
              </button>
            </form>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-xs font-semibold uppercase dark:border-slate-700 disabled:opacity-50"
              type="button"
              onClick={() => exportAuditLogs("csv")}
              disabled={exporting !== null}
            >
              {exporting === "csv" ? "Exporting CSV…" : "Export CSV"}
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-xs font-semibold uppercase dark:border-slate-700 disabled:opacity-50"
              type="button"
              onClick={() => exportAuditLogs("json")}
              disabled={exporting !== null}
            >
              {exporting === "json" ? "Exporting JSON…" : "Export JSON"}
            </button>
          </div>
        </div>

        {loading ? <p className="text-sm text-slate-500">Loading audit logs…</p> : null}
        <div className="overflow-auto">
          <table className="data-table min-w-[1080px]">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Action</th>
                <th>Object</th>
                <th>Actor</th>
                <th>Project</th>
                <th>Metadata</th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 ? (
                <tr>
                  <td className="text-sm text-slate-500" colSpan={6}>
                    No audit events found.
                  </td>
                </tr>
              ) : (
                events.map((event) => (
                  <tr key={event.id}>
                    <td className="text-xs">{formatTime(event.ts)}</td>
                    <td>
                      <div className="font-semibold">{event.action}</div>
                    </td>
                    <td className="text-xs">
                      <div>{event.object_type}</div>
                      <div className="text-slate-500">{event.object_id}</div>
                    </td>
                    <td className="text-xs">{event.actor_email || event.actor_user_id || "system"}</td>
                    <td className="text-xs">{event.project_name || event.project_id || "global"}</td>
                    <td className="max-w-[400px] text-xs">
                      <pre className="whitespace-pre-wrap break-words text-[11px]">{JSON.stringify(event.metadata || {}, null, 2)}</pre>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center gap-2">
          <button
            className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
            type="button"
            onClick={previousPage}
            disabled={history.length === 0}
          >
            Previous
          </button>
          <button
            className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
            type="button"
            onClick={nextPage}
            disabled={!nextCursor}
          >
            Next
          </button>
        </div>
      </div>
    </>
  );
}
