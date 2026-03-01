import { FormEvent, useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { apiFetch } from "@/lib/api";
import type { SettingsOutletContext } from "@/pages/settings-layout";

type UserRow = {
  id: string;
  email: string;
  is_active: boolean;
  is_sysadmin: boolean;
  is_approved: boolean;
  created_at: string;
};

export function SettingsUsersPage() {
  const { me } = useOutletContext<SettingsOutletContext>();

  const [users, setUsers] = useState<UserRow[]>([]);
  const [search, setSearch] = useState("");
  const [pendingOnly, setPendingOnly] = useState(false);
  const [loading, setLoading] = useState(false);

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(true);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadUsers() {
    setLoading(true);
    const query = new URLSearchParams({ limit: "200" });
    if (search.trim()) query.set("search", search.trim());
    if (pendingOnly) query.set("include_pending_only", "true");
    try {
      const data = await apiFetch(`/users?${query.toString()}`);
      setUsers((data?.items || []) as UserRow[]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadUsers().catch((err) => setError(err instanceof Error ? err.message : "Failed to load users"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, pendingOnly]);

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
          is_approved: newUserApproved,
        }),
      });
      setInfo("User created.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(true);
      await loadUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    }
  }

  async function patchUser(userId: string, payload: Record<string, unknown>, successMessage: string) {
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/users/${userId}`, { method: "PATCH", body: JSON.stringify(payload) });
      setInfo(successMessage);
      await loadUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    }
  }

  return (
    <>
      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 md:grid-cols-2">
        <div className="workspace-card space-y-3">
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
            <label className="flex items-center gap-2 text-sm">
              <input checked={newUserApproved} onChange={(event) => setNewUserApproved(event.target.checked)} type="checkbox" />
              Mark approved
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Create user
            </button>
          </form>
        </div>

        <div className="workspace-card space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">User Directory</h2>
            <div className="flex items-center gap-2">
              <input
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search email"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <label className="flex items-center gap-1 text-xs">
                <input checked={pendingOnly} onChange={(event) => setPendingOnly(event.target.checked)} type="checkbox" />
                Pending only
              </label>
            </div>
          </div>
          {loading ? <p className="text-sm text-slate-500">Loading users…</p> : null}
          <ul className="max-h-[420px] space-y-2 overflow-auto text-sm">
            {users.map((user) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={user.id}>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="font-semibold">{user.email}</div>
                    <div className="text-xs text-slate-500">
                      {user.is_sysadmin ? "sysadmin" : "user"} | {user.is_active ? "active" : "disabled"} |{" "}
                      {user.is_approved ? "approved" : "pending approval"}
                    </div>
                    <div className="text-xs text-slate-500">Created: {new Date(user.created_at).toLocaleString()}</div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      disabled={user.id === me.id}
                      onClick={() => patchUser(user.id, { is_active: !user.is_active }, user.is_active ? "User disabled." : "User enabled.")}
                    >
                      {user.is_active ? "Disable" : "Enable"}
                    </button>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      disabled={user.id === me.id}
                      onClick={() =>
                        patchUser(
                          user.id,
                          { is_approved: !user.is_approved },
                          user.is_approved ? "User unapproved." : "User approved.",
                        )
                      }
                    >
                      {user.is_approved ? "Unapprove" : "Approve"}
                    </button>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      disabled={user.id === me.id}
                      onClick={() =>
                        patchUser(
                          user.id,
                          { is_sysadmin: !user.is_sysadmin },
                          user.is_sysadmin ? "Sysadmin removed." : "Sysadmin granted.",
                        )
                      }
                    >
                      {user.is_sysadmin ? "Demote" : "Promote"}
                    </button>
                  </div>
                </div>
                {user.id === me.id ? <p className="mt-2 text-xs text-amber-700">Self-protection rules prevent lockout actions on your own account.</p> : null}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}
