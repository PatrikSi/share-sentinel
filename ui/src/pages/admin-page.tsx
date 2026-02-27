import { FormEvent, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type Project = { id: string; name: string };
type TokenMeta = { id: string; name: string; role: string; revoked_at: string | null };
type AuditEvent = { id: number; ts: string; action: string; object_type: string; object_id: string };
type Member = { user_id: string; email: string; role: string };

export function AdminPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [members, setMembers] = useState<Member[]>([]);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [tokens, setTokens] = useState<TokenMeta[]>([]);

  const [newMemberUserId, setNewMemberUserId] = useState("");
  const [newMemberRole, setNewMemberRole] = useState("viewer");
  const [tokenName, setTokenName] = useState("collector-token");
  const [tokenRole, setTokenRole] = useState("operator");
  const [createdToken, setCreatedToken] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/projects").then((data) => {
      const rows = (data || []) as Project[];
      setProjects(rows);
      if (rows.length > 0) {
        setProjectId(rows[0].id);
      }
    });
    apiFetch("/auth/api-tokens").then((data) => setTokens((data || []) as TokenMeta[]));
  }, []);

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/projects/${projectId}/members`).then((data) => setMembers((data?.items || []) as Member[]));
    apiFetch(`/projects/${projectId}/audit?limit=50`).then((data) => setAudit((data?.items || []) as AuditEvent[]));
  }, [projectId]);

  async function addMember(event: FormEvent) {
    event.preventDefault();
    await apiFetch(`/projects/${projectId}/members`, {
      method: "POST",
      body: JSON.stringify({ user_id: newMemberUserId, role: newMemberRole }),
    });
    const data = await apiFetch(`/projects/${projectId}/members`);
    setMembers((data?.items || []) as Member[]);
  }

  async function createToken(event: FormEvent) {
    event.preventDefault();
    const data = await apiFetch("/auth/api-tokens", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, name: tokenName, role: tokenRole }),
    });
    setCreatedToken(data.token as string);
    setTokens((prev) => [data.token_meta as TokenMeta, ...prev]);
  }

  async function revokeToken(tokenId: string) {
    await apiFetch(`/auth/api-tokens/${tokenId}`, { method: "DELETE" });
    setTokens((prev) => prev.map((token) => (token.id === tokenId ? { ...token, revoked_at: new Date().toISOString() } : token)));
  }

  return (
    <section className="space-y-6">
      <div className="panel flex items-center justify-between">
        <h1 className="text-2xl font-bold">Admin</h1>
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
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="panel space-y-4">
          <h2 className="text-lg font-semibold">Members</h2>
          <form className="flex flex-wrap gap-2" onSubmit={addMember}>
            <input
              className="min-w-64 flex-1 rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="User UUID"
              value={newMemberUserId}
              onChange={(event) => setNewMemberUserId(event.target.value)}
              required
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={newMemberRole}
              onChange={(event) => setNewMemberRole(event.target.value)}
            >
              <option value="admin">admin</option>
              <option value="operator">operator</option>
              <option value="viewer">viewer</option>
            </select>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Add
            </button>
          </form>

          <ul className="space-y-2 text-sm">
            {members.map((member) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={member.user_id}>
                <div className="font-mono text-xs">{member.user_id}</div>
                <div>{member.email}</div>
                <div className="text-slate-500">{member.role}</div>
              </li>
            ))}
          </ul>
        </div>

        <div className="panel space-y-4">
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
            <button className="rounded-lg bg-ember px-3 py-1 text-sm font-semibold text-white" type="submit">
              Create
            </button>
          </form>

          {createdToken ? (
            <p className="rounded-lg bg-amber-100 p-2 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
              Copy now (shown once): {createdToken}
            </p>
          ) : null}

          <ul className="space-y-2 text-sm">
            {tokens.map((token) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={token.id}>
                <div className="font-semibold">{token.name}</div>
                <div className="font-mono text-xs">{token.id}</div>
                <div className="text-slate-500">role: {token.role}</div>
                <button
                  className="mt-1 rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                  onClick={() => revokeToken(token.id)}
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="panel">
        <h2 className="mb-3 text-lg font-semibold">Audit</h2>
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
