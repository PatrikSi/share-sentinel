import { FormEvent, useEffect, useMemo, useState } from "react";

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

type UserOption = { id: string; email: string };
type ProjectOption = { id: string; name: string };
type ScopeCatalog = {
  allowed_scopes: string[];
  defaults_by_role: Record<string, string[]>;
};

type TokenCreateResponse = {
  token: string;
  token_meta: ApiTokenRow;
};

const ROLES = ["viewer", "operator", "admin"];

function formatTime(value: string | null): string {
  if (!value) return "N/A";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "N/A";
  return date.toLocaleString();
}

function parseScopesCsv(raw: string): string[] {
  return raw
    .split(",")
    .map((scope) => scope.trim().toLowerCase())
    .filter(Boolean);
}

export function SettingsApiTokensPage() {
  const [tokens, setTokens] = useState<ApiTokenRow[]>([]);
  const [users, setUsers] = useState<UserOption[]>([]);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [scopeCatalog, setScopeCatalog] = useState<ScopeCatalog | null>(null);

  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [createUserId, setCreateUserId] = useState("");
  const [createProjectId, setCreateProjectId] = useState("");
  const [createName, setCreateName] = useState("collector-token");
  const [createRole, setCreateRole] = useState("operator");
  const [createUseDefaults, setCreateUseDefaults] = useState(true);
  const [createScopesCsv, setCreateScopesCsv] = useState("");
  const [createExpiryDays, setCreateExpiryDays] = useState("90");

  const [editTokenId, setEditTokenId] = useState("");
  const [editName, setEditName] = useState("");
  const [editRole, setEditRole] = useState("operator");
  const [editUseDefaults, setEditUseDefaults] = useState(true);
  const [editScopesCsv, setEditScopesCsv] = useState("");
  const [editExpiryDays, setEditExpiryDays] = useState("");
  const [editNeverExpires, setEditNeverExpires] = useState(false);

  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [rotatedSecret, setRotatedSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const editToken = useMemo(() => tokens.find((token) => token.id === editTokenId) || null, [tokens, editTokenId]);
  const tokenStats = useMemo(() => {
    const total = tokens.length;
    const active = tokens.filter((token) => !token.revoked_at).length;
    const revoked = total - active;
    return { total, active, revoked };
  }, [tokens]);

  async function loadReferenceData() {
    const [usersData, projectsData, scopeData] = await Promise.all([
      apiFetch("/users?limit=500"),
      apiFetch("/settings/projects"),
      apiFetch("/settings/api-token-scopes"),
    ]);
    const userRows = ((usersData?.items as UserOption[]) || []).sort((a, b) => a.email.localeCompare(b.email));
    const projectRows = (projectsData || []) as ProjectOption[];

    setUsers(userRows);
    setProjects(projectRows);
    setScopeCatalog(scopeData as ScopeCatalog);

    if (!createUserId && userRows.length > 0) setCreateUserId(userRows[0].id);
    if (!createProjectId && projectRows.length > 0) setCreateProjectId(projectRows[0].id);
  }

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
    loadReferenceData().catch((err) => setError(err instanceof Error ? err.message : "Failed to load token metadata"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadTokens().catch((err) => setError(err instanceof Error ? err.message : "Failed to load API tokens"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  useEffect(() => {
    if (!editToken) return;
    setEditName(editToken.name);
    setEditRole(editToken.role);
    setEditScopesCsv(editToken.scopes.join(", "));
    setEditUseDefaults(false);
    setEditExpiryDays("");
    setEditNeverExpires(false);
  }, [editToken]);

  async function createToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!createUserId || !createProjectId) return;
    setError(null);
    setInfo(null);
    setCreatedSecret(null);
    setRotatedSecret(null);

    const expiryDays = createExpiryDays.trim() ? Number.parseInt(createExpiryDays.trim(), 10) : Number.NaN;
    const scopes = createUseDefaults ? [] : parseScopesCsv(createScopesCsv);

    try {
      const data = (await apiFetch("/settings/api-tokens", {
        method: "POST",
        body: JSON.stringify({
          user_id: createUserId,
          project_id: createProjectId,
          name: createName,
          role: createRole,
          expires_in_days: Number.isFinite(expiryDays) && expiryDays > 0 ? expiryDays : undefined,
          scopes,
        }),
      })) as TokenCreateResponse;
      setCreatedSecret(data.token);
      setInfo("API token created.");
      setTokens((prev) => [data.token_meta, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create token");
    }
  }

  async function saveTokenUpdates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editToken) return;
    setError(null);
    setInfo(null);
    setCreatedSecret(null);
    setRotatedSecret(null);

    const expiryDays = editExpiryDays.trim() ? Number.parseInt(editExpiryDays.trim(), 10) : Number.NaN;
    const scopes = editUseDefaults ? [] : parseScopesCsv(editScopesCsv);

    try {
      const data = (await apiFetch(`/settings/api-tokens/${editToken.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editName,
          role: editRole,
          scopes,
          expires_in_days: Number.isFinite(expiryDays) && expiryDays > 0 ? expiryDays : undefined,
          never_expires: editNeverExpires,
        }),
      })) as ApiTokenRow;
      setInfo("API token updated.");
      setTokens((prev) => prev.map((token) => (token.id === data.id ? data : token)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update token");
    }
  }

  async function rotateToken(tokenId: string) {
    if (!window.confirm("Rotate this API token secret? Existing secret will stop working.")) return;
    setError(null);
    setInfo(null);
    setCreatedSecret(null);
    try {
      const data = (await apiFetch(`/settings/api-tokens/${tokenId}/rotate`, { method: "POST" })) as TokenCreateResponse;
      setRotatedSecret(data.token);
      setInfo("API token rotated.");
      setTokens((prev) => prev.map((token) => (token.id === data.token_meta.id ? data.token_meta : token)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rotate token");
    }
  }

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
      {error || info || createdSecret || rotatedSecret ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
          {createdSecret ? (
            <p className="rounded-xl bg-amber-100 p-3 text-xs text-amber-900 dark:bg-amber-900/30 dark:text-amber-200">
              New token secret (shown once): <code>{createdSecret}</code>
            </p>
          ) : null}
          {rotatedSecret ? (
            <p className="rounded-xl bg-amber-100 p-3 text-xs text-amber-900 dark:bg-amber-900/30 dark:text-amber-200">
              Rotated token secret (shown once): <code>{rotatedSecret}</code>
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 lg:grid-cols-2">
        <div className="workspace-card space-y-3">
          <h2 className="text-lg font-semibold">Create API Token</h2>
          <form className="space-y-3" onSubmit={createToken}>
            <label className="block text-sm">
              Owner
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={createUserId}
                onChange={(event) => setCreateUserId(event.target.value)}
              >
                {users.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Project
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={createProjectId}
                onChange={(event) => setCreateProjectId(event.target.value)}
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Name
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={createName}
                onChange={(event) => setCreateName(event.target.value)}
                minLength={1}
                maxLength={120}
                required
              />
            </label>
            <label className="block text-sm">
              Role
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={createRole}
                onChange={(event) => setCreateRole(event.target.value)}
              >
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Expiry days
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={createExpiryDays}
                onChange={(event) => setCreateExpiryDays(event.target.value)}
                placeholder="90"
              />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input checked={createUseDefaults} type="checkbox" onChange={(event) => setCreateUseDefaults(event.target.checked)} />
              Use default scopes for selected role
            </label>
            {!createUseDefaults ? (
              <label className="block text-sm">
                Custom scopes (CSV)
                <input
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={createScopesCsv}
                  onChange={(event) => setCreateScopesCsv(event.target.value)}
                  placeholder="read:projects, read:runs"
                />
              </label>
            ) : null}
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Create token
            </button>
          </form>
        </div>

        <div className="workspace-card space-y-3">
          <h2 className="text-lg font-semibold">Scope Catalog</h2>
          <p className="text-xs text-slate-500">Allowed scopes and role defaults used for token policy validation.</p>
          <div className="grid gap-2 text-xs md:grid-cols-2">
            <div>
              <p className="mb-1 font-semibold">Role defaults</p>
              {(scopeCatalog?.defaults_by_role ? Object.entries(scopeCatalog.defaults_by_role) : []).map(([role, scopes]) => (
                <div className="mb-2" key={role}>
                  <p className="font-semibold">{role}</p>
                  <p className="text-slate-500">{scopes.join(", ") || "none"}</p>
                </div>
              ))}
            </div>
            <div>
              <p className="mb-1 font-semibold">All supported scopes</p>
              <p className="max-h-[200px] overflow-auto whitespace-pre-wrap break-words text-slate-500">
                {scopeCatalog?.allowed_scopes.join(", ") || "loading..."}
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className="workspace-section space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Global API Tokens</h2>
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span>Total: {tokenStats.total}</span>
            <span>Active: {tokenStats.active}</span>
            <span>Revoked: {tokenStats.revoked}</span>
          </div>
        </div>

        <form className="flex flex-wrap items-center gap-2" onSubmit={onSearch}>
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

        {loading ? <p className="text-sm text-slate-500">Loading API tokens…</p> : null}
        <div className="overflow-auto">
          <table className="data-table min-w-[1100px]">
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
                    <td className="max-w-[240px] text-xs">{token.scopes.join(", ") || "none"}</td>
                    <td className="text-xs">{formatTime(token.created_at)}</td>
                    <td className="text-xs">{formatTime(token.last_used_at)}</td>
                    <td className="text-xs">{formatTime(token.expires_at)}</td>
                    <td className="text-xs">{token.revoked_at ? "revoked" : "active"}</td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        <button
                          className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                          onClick={() => setEditTokenId(token.id)}
                          type="button"
                        >
                          Edit
                        </button>
                        <button
                          className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
                          disabled={!!token.revoked_at}
                          onClick={() => rotateToken(token.id)}
                          type="button"
                        >
                          Rotate
                        </button>
                        <button
                          className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
                          disabled={!!token.revoked_at}
                          onClick={() => revokeToken(token.id)}
                          type="button"
                        >
                          Revoke
                        </button>
                      </div>
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

      {editToken ? (
        <div className="workspace-section">
          <div className="workspace-card space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-lg font-semibold">Edit Token</h2>
              <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onClick={() => setEditTokenId("")}>
                Close
              </button>
            </div>
            <p className="text-xs text-slate-500">Editing {editToken.name} ({editToken.id})</p>
            <form className="grid gap-3 md:grid-cols-2" onSubmit={saveTokenUpdates}>
              <label className="block text-sm">
                Name
                <input
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={editName}
                  onChange={(event) => setEditName(event.target.value)}
                  minLength={1}
                  maxLength={120}
                  required
                />
              </label>
              <label className="block text-sm">
                Role
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={editRole}
                  onChange={(event) => setEditRole(event.target.value)}
                >
                  {ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-sm">
                New expiry days (leave empty to keep)
                <input
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={editExpiryDays}
                  onChange={(event) => setEditExpiryDays(event.target.value)}
                  placeholder="180"
                  disabled={editNeverExpires}
                />
              </label>
              <label className="flex items-center gap-2 text-sm md:mt-7">
                <input checked={editNeverExpires} type="checkbox" onChange={(event) => setEditNeverExpires(event.target.checked)} />
                Set token to never expire
              </label>
              <label className="flex items-center gap-2 text-sm md:col-span-2">
                <input checked={editUseDefaults} type="checkbox" onChange={(event) => setEditUseDefaults(event.target.checked)} />
                Reset scopes to role defaults
              </label>
              {!editUseDefaults ? (
                <label className="block text-sm md:col-span-2">
                  Scopes (CSV)
                  <input
                    className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                    value={editScopesCsv}
                    onChange={(event) => setEditScopesCsv(event.target.value)}
                  />
                </label>
              ) : null}
              <div className="md:col-span-2">
                <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
                  Save token changes
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </>
  );
}
