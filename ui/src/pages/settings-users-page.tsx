import { FormEvent, useEffect, useMemo, useState } from "react";
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

const PROJECT_ROLES = ["viewer", "operator", "admin"];

export function SettingsUsersPage() {
  const { me } = useOutletContext<SettingsOutletContext>();

  const [users, setUsers] = useState<UserRow[]>([]);
  const [search, setSearch] = useState("");
  const [pendingOnly, setPendingOnly] = useState(false);
  const [isActiveFilter, setIsActiveFilter] = useState("all");
  const [isApprovedFilter, setIsApprovedFilter] = useState("all");
  const [isSysadminFilter, setIsSysadminFilter] = useState("all");
  const [loading, setLoading] = useState(false);

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(true);
  const [newUserAllProjects, setNewUserAllProjects] = useState(false);
  const [newUserAllProjectsRole, setNewUserAllProjectsRole] = useState("viewer");

  const [assignUserId, setAssignUserId] = useState("");
  const [assignRole, setAssignRole] = useState("viewer");
  const [assignOverwrite, setAssignOverwrite] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const counts = useMemo(() => {
    const total = users.length;
    const active = users.filter((user) => user.is_active).length;
    const pending = users.filter((user) => !user.is_approved).length;
    const admins = users.filter((user) => user.is_sysadmin).length;
    return { total, active, pending, admins };
  }, [users]);

  async function loadUsers() {
    setLoading(true);
    const query = new URLSearchParams({ limit: "500" });
    if (search.trim()) query.set("search", search.trim());
    if (pendingOnly) query.set("include_pending_only", "true");
    if (isActiveFilter !== "all") query.set("is_active", isActiveFilter);
    if (isApprovedFilter !== "all") query.set("is_approved", isApprovedFilter);
    if (isSysadminFilter !== "all") query.set("is_sysadmin", isSysadminFilter);
    try {
      const data = await apiFetch(`/users?${query.toString()}`);
      const rows = (data?.items || []) as UserRow[];
      setUsers(rows);
      if (!assignUserId && rows.length > 0) {
        setAssignUserId(rows[0].id);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadUsers().catch((err) => setError(err instanceof Error ? err.message : "Failed to load users"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, pendingOnly, isActiveFilter, isApprovedFilter, isSysadminFilter]);

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
          add_to_all_projects: newUserAllProjects,
          all_projects_role: newUserAllProjectsRole,
        }),
      });
      setInfo("User created.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(true);
      setNewUserAllProjects(false);
      setNewUserAllProjectsRole("viewer");
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

  async function assignToAllProjects(userId: string, role: string, overwriteExisting: boolean) {
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/users/${userId}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role, overwrite_existing: overwriteExisting }),
      });
      const assigned = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      setInfo(`Assigned user to ${assigned} project memberships.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to assign memberships");
    }
  }

  async function resetPassword(user: UserRow) {
    const nextPassword = window.prompt(`Set temporary password for ${user.email} (minimum 12 characters):`);
    if (!nextPassword) return;
    if (nextPassword.length < 12) {
      setError("Temporary password must be at least 12 characters.");
      return;
    }
    await patchUser(user.id, { password: nextPassword }, "Password updated.");
  }

  return (
    <>
      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p> : null}
          {info ? <p className="rounded-xl bg-emerald-100 p-3 text-sm text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200">{info}</p> : null}
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 xl:grid-cols-3">
        <div className="workspace-card space-y-3 xl:col-span-1">
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
            <label className="flex items-center gap-2 text-sm">
              <input checked={newUserAllProjects} onChange={(event) => setNewUserAllProjects(event.target.checked)} type="checkbox" />
              Add to all projects on create
            </label>
            {newUserAllProjects ? (
              <label className="block text-sm">
                All-project role
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={newUserAllProjectsRole}
                  onChange={(event) => setNewUserAllProjectsRole(event.target.value)}
                >
                  {PROJECT_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Create user
            </button>
          </form>
        </div>

        <div className="workspace-card space-y-3 xl:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">User Directory</h2>
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span>Total: {counts.total}</span>
              <span>Active: {counts.active}</span>
              <span>Pending: {counts.pending}</span>
              <span>Sysadmin: {counts.admins}</span>
            </div>
          </div>

          <div className="grid gap-2 lg:grid-cols-5">
            <input
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search email"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={isActiveFilter}
              onChange={(event) => setIsActiveFilter(event.target.value)}
            >
              <option value="all">All activity</option>
              <option value="true">Active only</option>
              <option value="false">Disabled only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={isApprovedFilter}
              onChange={(event) => setIsApprovedFilter(event.target.value)}
            >
              <option value="all">All approvals</option>
              <option value="true">Approved only</option>
              <option value="false">Pending only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={isSysadminFilter}
              onChange={(event) => setIsSysadminFilter(event.target.value)}
            >
              <option value="all">All roles</option>
              <option value="true">Sysadmin only</option>
              <option value="false">Non-sysadmin</option>
            </select>
            <label className="flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700">
              <input checked={pendingOnly} onChange={(event) => setPendingOnly(event.target.checked)} type="checkbox" />
              Pending only quick filter
            </label>
          </div>

          <div className="rounded-lg border border-slate-200 p-2 dark:border-slate-700">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Assign Existing User To All Projects</p>
            <div className="grid gap-2 md:grid-cols-4">
              <select
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                value={assignUserId}
                onChange={(event) => setAssignUserId(event.target.value)}
              >
                {users.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.email}
                  </option>
                ))}
              </select>
              <select
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                value={assignRole}
                onChange={(event) => setAssignRole(event.target.value)}
              >
                {PROJECT_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700">
                <input checked={assignOverwrite} onChange={(event) => setAssignOverwrite(event.target.checked)} type="checkbox" />
                Overwrite existing memberships
              </label>
              <button
                className="rounded-lg bg-pine px-3 py-1 text-xs font-semibold text-white"
                type="button"
                onClick={() => {
                  if (!assignUserId) return;
                  assignToAllProjects(assignUserId, assignRole, assignOverwrite).catch((err) =>
                    setError(err instanceof Error ? err.message : "Failed to assign memberships"),
                  );
                }}
              >
                Assign now
              </button>
            </div>
          </div>

          {loading ? <p className="text-sm text-slate-500">Loading users…</p> : null}
          <ul className="max-h-[520px] space-y-2 overflow-auto text-sm">
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
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      onClick={() => {
                        assignToAllProjects(user.id, "viewer", false).catch((err) =>
                          setError(err instanceof Error ? err.message : "Failed to assign memberships"),
                        );
                      }}
                    >
                      Add To All Projects
                    </button>
                    <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onClick={() => resetPassword(user)}>
                      Reset Password
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
