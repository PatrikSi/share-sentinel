import { FormEvent, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type ApiTokenRow = {
  id: string;
  user_id: string;
  user_email: string;
  project_id: string;
  project_name: string;
  name: string;
  role: string;
  scopes: string[];
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
};

function formatTime(value: string | null): string {
  if (!value) return "N/A";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "N/A";
  return date.toLocaleString();
}

export function SettingsApiTokensPage() {
  const [tokens, setTokens] = useState<ApiTokenRow[]>([]);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadTokens() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/api-tokens?${query.toString()}`);
      setTokens((data?.items || []) as ApiTokenRow[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadTokens().catch((err) => setError(err instanceof Error ? err.message : "Failed to load API tokens"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  async function revokeToken(tokenId: string) {
    if (!window.confirm("Revoke this API token?")) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/api-tokens/${tokenId}`, { method: "DELETE" });
      setInfo("API token revoked.");
      setTokens((prev) => prev.map((token) => (token.id === tokenId ? { ...token, revoked_at: new Date().toISOString() } : token)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke token");
    }
  }

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

  return (
    <>
      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Global API Tokens</h2>
          <form className="flex items-center gap-2" onSubmit={onSearch}>
            <input
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search token, user, project, id"
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
        </div>

        {loading ? <p className="text-sm text-slate-500">Loading API tokens…</p> : null}
        <div className="overflow-auto">
          <table className="data-table min-w-[960px]">
            <thead>
              <tr>
                <th>Name</th>
                <th>Project</th>
                <th>Owner</th>
                <th>Role</th>
                <th>Scopes</th>
                <th>Created</th>
                <th>Last Used</th>
                <th>Expires</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {tokens.length === 0 ? (
                <tr>
                  <td className="text-sm text-slate-500" colSpan={10}>
                    No tokens found.
                  </td>
                </tr>
              ) : (
                tokens.map((token) => (
                  <tr key={token.id}>
                    <td>
                      <div className="font-semibold">{token.name}</div>
                      <div className="text-xs text-slate-500">{token.id}</div>
                    </td>
                    <td>{token.project_name}</td>
                    <td>{token.user_email}</td>
                    <td>{token.role}</td>
                    <td className="max-w-[280px] text-xs">{token.scopes.join(", ") || "none"}</td>
                    <td className="text-xs">{formatTime(token.created_at)}</td>
                    <td className="text-xs">{formatTime(token.last_used_at)}</td>
                    <td className="text-xs">{formatTime(token.expires_at)}</td>
                    <td className="text-xs">{token.revoked_at ? "revoked" : "active"}</td>
                    <td>
                      <button
                        className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
                        disabled={!!token.revoked_at}
                        onClick={() => revokeToken(token.id)}
                      >
                        Revoke
                      </button>
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
