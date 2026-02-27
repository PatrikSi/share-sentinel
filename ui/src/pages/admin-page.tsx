import { FormEvent, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type UserMe = { id: string; is_sysadmin: boolean; email: string };
type Project = { id: string; name: string };
type TokenMeta = { id: string; name: string; role: string; revoked_at: string | null; created_at: string };
type AuditEvent = { id: number; ts: string; action: string; object_type: string; object_id: string };
type Member = { user_id: string; email: string; role: string };
type UserRow = { id: string; email: string; is_active: boolean; is_sysadmin: boolean; created_at: string };

export function AdminPage() {
  const [me, setMe] = useState<UserMe | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [projectRole, setProjectRole] = useState<string | null>(null);

  const [members, setMembers] = useState<Member[]>([]);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [tokens, setTokens] = useState<TokenMeta[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const [memberEmail, setMemberEmail] = useState("");
  const [memberRole, setMemberRole] = useState("viewer");

  const [tokenName, setTokenName] = useState("collector-token");
  const [tokenRole, setTokenRole] = useState("operator");
  const [createdToken, setCreatedToken] = useState<string | null>(null);

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [userSearch, setUserSearch] = useState("");

  const [auditCursor, setAuditCursor] = useState<string | null>(null);
  const [auditHistory, setAuditHistory] = useState<Array<string | null>>([]);
  const [auditNext, setAuditNext] = useState<string | null>(null);

  async function loadProjectContext(selectedId: string) {
    if (!selectedId) return;
    const roleData = await apiFetch(`/projects/${selectedId}/my-role`);
    setProjectRole((roleData?.role as string) || null);

    const membersData = await apiFetch(`/projects/${selectedId}/members`);
    setMembers((membersData?.items || []) as Member[]);

    const auditData = await apiFetch(`/projects/${selectedId}/audit?limit=50`);
    setAudit((auditData?.items || []) as AuditEvent[]);
    setAuditNext((auditData?.next_cursor as string | null) || null);
    setAuditCursor(null);
    setAuditHistory([]);
  }

  async function loadUserTokens() {
    const tokenData = await apiFetch("/auth/api-tokens");
    setTokens((tokenData || []) as TokenMeta[]);
  }

  async function loadUsers(search: string) {
    const query = new URLSearchParams({ limit: "100" });
    if (search.trim()) {
      query.set("search", search.trim());
    }
    const data = await apiFetch(`/users?${query.toString()}`);
    setUsers((data?.items || []) as UserRow[]);
  }

  useEffect(() => {
    apiFetch("/auth/me")
      .then((data) => setMe(data as UserMe))
      .catch((err) => setError(err.message));

    apiFetch("/projects")
      .then(async (data) => {
        const rows = (data || []) as Project[];
        setProjects(rows);
        if (rows.length > 0) {
          setProjectId(rows[0].id);
          await loadProjectContext(rows[0].id);
        }
      })
      .catch((err) => setError(err.message));

    loadUserTokens().catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (!projectId) return;
    loadProjectContext(projectId).catch((err) => setError(err.message));
  }, [projectId]);

  useEffect(() => {
    if (!me?.is_sysadmin) return;
    loadUsers(userSearch).catch((err) => setError(err.message));
  }, [me?.is_sysadmin, userSearch]);

  async function refreshMembers() {
    if (!projectId) return;
    const data = await apiFetch(`/projects/${projectId}/members`);
    setMembers((data?.items || []) as Member[]);
  }

  async function addMember(event: FormEvent) {
    event.preventDefault();
    if (!projectId || !memberEmail.trim()) return;
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/projects/${projectId}/members/by-email`, {
        method: "POST",
        body: JSON.stringify({ email: memberEmail.trim().toLowerCase(), role: memberRole }),
      });
      setInfo(`Member upserted: ${(data?.email as string) || memberEmail}`);
      setMemberEmail("");
      await refreshMembers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add member");
    }
  }

  async function removeMember(userId: string) {
    if (!projectId) return;
    if (!window.confirm("Remove member from project?")) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/projects/${projectId}/members/${userId}`, { method: "DELETE" });
      setInfo("Member removed.");
      await refreshMembers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove member");
    }
  }

  async function createToken(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    setError(null);
    setInfo(null);
    setCreatedToken(null);
    try {
      const data = await apiFetch("/auth/api-tokens", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, name: tokenName, role: tokenRole }),
      });
      setCreatedToken(data.token as string);
      setTokens((prev) => [data.token_meta as TokenMeta, ...prev]);
      setInfo("API token created.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create token");
    }
  }

  async function revokeToken(tokenId: string) {
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/auth/api-tokens/${tokenId}`, { method: "DELETE" });
      setTokens((prev) => prev.map((token) => (token.id === tokenId ? { ...token, revoked_at: new Date().toISOString() } : token)));
      setInfo("Token revoked.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke token");
    }
  }

  async function createUser(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/users", {
        method: "POST",
        body: JSON.stringify({
          email: newUserEmail.trim().toLowerCase(),
          password: newUserPassword,
          is_active: true,
          is_sysadmin: newUserSysadmin,
        }),
      });
      setInfo("User created.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      await loadUsers(userSearch);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    }
  }

  async function setUserActive(userId: string, isActive: boolean) {
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/users/${userId}/status?is_active=${isActive ? "true" : "false"}`, { method: "PATCH" });
      setUsers((prev) => prev.map((user) => (user.id === userId ? { ...user, is_active: isActive } : user)));
      setInfo(`User ${isActive ? "enabled" : "disabled"}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update user status");
    }
  }

  async function nextAuditPage() {
    if (!auditNext || !projectId) return;
    setAuditHistory((prev) => [...prev, auditCursor]);
    setAuditCursor(auditNext);
    const data = await apiFetch(`/projects/${projectId}/audit?limit=50&cursor=${encodeURIComponent(auditNext)}`);
    setAudit((data?.items || []) as AuditEvent[]);
    setAuditNext((data?.next_cursor as string | null) || null);
  }

  async function prevAuditPage() {
    if (!projectId || auditHistory.length === 0) return;
    const copy = [...auditHistory];
    const previous = copy.pop() ?? null;
    setAuditHistory(copy);
    setAuditCursor(previous);
    const query = new URLSearchParams({ limit: "50" });
    if (previous) query.set("cursor", previous);
    const data = await apiFetch(`/projects/${projectId}/audit?${query.toString()}`);
    setAudit((data?.items || []) as AuditEvent[]);
    setAuditNext((data?.next_cursor as string | null) || null);
  }

  const canProjectAdmin = projectRole === "admin";

  return (
    <section className="workspace">
      <div className="workspace-header flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Administration</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">Manage members, tokens, users, and audit history.</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            className="rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <span className="text-xs text-slate-500">Role: {projectRole || "unknown"}</span>
        </div>
      </div>

      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 md:grid-cols-2">
        <div className="workspace-card space-y-4">
          <h2 className="text-lg font-semibold">Project Members</h2>
          <form className="flex flex-wrap gap-2" onSubmit={addMember}>
            <input
              className="min-w-56 flex-1 rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="user@example.com"
              type="email"
              value={memberEmail}
              onChange={(event) => setMemberEmail(event.target.value)}
              required
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={memberRole}
              onChange={(event) => setMemberRole(event.target.value)}
            >
              <option value="admin">admin</option>
              <option value="operator">operator</option>
              <option value="viewer">viewer</option>
            </select>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit" disabled={!canProjectAdmin}>
              Upsert
            </button>
          </form>
          {!canProjectAdmin ? <p className="text-xs text-amber-700">Project admin role required for member changes.</p> : null}

          <ul className="space-y-2 text-sm">
            {members.map((member) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={member.user_id}>
                <div className="font-semibold">{member.email}</div>
                <div className="font-mono text-xs">{member.user_id}</div>
                <div className="mt-1 flex items-center justify-between">
                  <span className="text-slate-500">{member.role}</span>
                  <button
                    className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 disabled:opacity-40 dark:border-rose-700 dark:text-rose-300"
                    onClick={() => removeMember(member.user_id)}
                    disabled={!canProjectAdmin || member.user_id === me?.id}
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <div className="workspace-card space-y-4">
          <h2 className="text-lg font-semibold">API Tokens</h2>
          <form className="flex flex-wrap gap-2" onSubmit={createToken}>
            <input
              className="min-w-40 flex-1 rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={tokenName}
              onChange={(event) => setTokenName(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={tokenRole}
              onChange={(event) => setTokenRole(event.target.value)}
            >
              <option value="admin">admin</option>
              <option value="operator">operator</option>
              <option value="viewer">viewer</option>
            </select>
            <button className="rounded-lg bg-ember px-3 py-1 text-sm font-semibold text-white" type="submit" disabled={!canProjectAdmin}>
              Create
            </button>
          </form>

          {createdToken ? (
            <div className="rounded-lg bg-amber-100 p-2 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
              <p className="font-semibold">Token (shown once)</p>
              <p className="font-mono break-all">{createdToken}</p>
              <p className="mt-2 text-[11px]">Collector example: `--api-token {createdToken}`</p>
            </div>
          ) : null}

          <ul className="space-y-2 text-sm">
            {tokens.map((token) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={token.id}>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="font-semibold">{token.name}</div>
                    <div className="font-mono text-xs">{token.id}</div>
                    <div className="text-slate-500">role: {token.role}</div>
                  </div>
                  <button
                    className="rounded border border-slate-300 px-2 py-1 text-xs disabled:opacity-40 dark:border-slate-700"
                    disabled={!!token.revoked_at}
                    onClick={() => revokeToken(token.id)}
                  >
                    {token.revoked_at ? "Revoked" : "Revoke"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {me?.is_sysadmin ? (
        <div className="workspace-section grid gap-4 md:grid-cols-2">
          <div className="workspace-card space-y-4">
            <h2 className="text-lg font-semibold">Create User</h2>
            <form className="space-y-3" onSubmit={createUser}>
              <input
                className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                placeholder="user@example.com"
                type="email"
                value={newUserEmail}
                onChange={(event) => setNewUserEmail(event.target.value)}
                required
              />
              <input
                className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                placeholder="Password (12+ chars)"
                type="password"
                value={newUserPassword}
                onChange={(event) => setNewUserPassword(event.target.value)}
                minLength={12}
                required
              />
              <label className="flex items-center gap-2 text-sm">
                <input checked={newUserSysadmin} onChange={(event) => setNewUserSysadmin(event.target.checked)} type="checkbox" />
                Grant sysadmin role
              </label>
              <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
                Create user
              </button>
            </form>
          </div>

          <div className="workspace-card space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold">User Directory</h2>
              <input
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search email"
                value={userSearch}
                onChange={(event) => setUserSearch(event.target.value)}
              />
            </div>
            <ul className="max-h-[320px] space-y-2 overflow-auto text-sm">
              {users.map((user) => (
                <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={user.id}>
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="font-semibold">{user.email}</div>
                      <div className="text-xs text-slate-500">
                        {user.is_sysadmin ? "sysadmin" : "user"} | {user.is_active ? "active" : "disabled"}
                      </div>
                    </div>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      onClick={() => setUserActive(user.id, !user.is_active)}
                    >
                      {user.is_active ? "Disable" : "Enable"}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      <div className="workspace-section">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Audit Events</h2>
          <div className="flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={prevAuditPage}
              disabled={auditHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-[10px] uppercase disabled:opacity-50 dark:border-slate-700"
              onClick={nextAuditPage}
              disabled={!auditNext}
            >
              Next
            </button>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Object</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((event) => (
              <tr key={event.id}>
                <td>{new Date(event.ts).toLocaleString()}</td>
                <td>{event.action}</td>
                <td>
                  {event.object_type}:{event.object_id}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
