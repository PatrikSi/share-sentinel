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

type Project = { id: string; name: string };

type Membership = {
  project_id: string;
  project_name: string;
  user_id: string;
  user_email: string;
  role: string;
};

const PROJECT_ROLES = ["viewer", "operator", "admin"];

function rolePillClass(role: string): string {
  if (role === "admin") return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200";
  if (role === "operator") return "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";
}

export function SettingsIamPage() {
  const { me } = useOutletContext<SettingsOutletContext>();

  const [users, setUsers] = useState<UserRow[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);

  const [usersLoading, setUsersLoading] = useState(false);
  const [membershipsLoading, setMembershipsLoading] = useState(false);

  const [userSearch, setUserSearch] = useState("");
  const [userActiveFilter, setUserActiveFilter] = useState("all");
  const [userApprovalFilter, setUserApprovalFilter] = useState("all");
  const [userSysadminFilter, setUserSysadminFilter] = useState("all");
  const [assignedProjectFilter, setAssignedProjectFilter] = useState("all");

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(true);
  const [newUserAllProjects, setNewUserAllProjects] = useState(false);
  const [newUserAllProjectsRole, setNewUserAllProjectsRole] = useState("viewer");

  const [membershipRoleDraft, setMembershipRoleDraft] = useState<Record<string, string>>({});
  const [rowProjectDraft, setRowProjectDraft] = useState<Record<string, string>>({});
  const [rowRoleDraft, setRowRoleDraft] = useState<Record<string, string>>({});
  const [rowAllProjectsRoleDraft, setRowAllProjectsRoleDraft] = useState<Record<string, string>>({});
  const [rowAllProjectsOverwrite, setRowAllProjectsOverwrite] = useState<Record<string, boolean>>({});

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const userStats = useMemo(() => {
    const total = users.length;
    const active = users.filter((user) => user.is_active).length;
    const pending = users.filter((user) => !user.is_approved).length;
    const sysadmins = users.filter((user) => user.is_sysadmin).length;
    return { total, active, pending, sysadmins };
  }, [users]);

  const membershipsByUserId = useMemo(() => {
    const grouped = new Map<string, Membership[]>();
    for (const membership of memberships) {
      const bucket = grouped.get(membership.user_id) || [];
      bucket.push(membership);
      grouped.set(membership.user_id, bucket);
    }
    for (const bucket of grouped.values()) {
      bucket.sort((a, b) => a.project_name.localeCompare(b.project_name));
    }
    return grouped;
  }, [memberships]);

  const visibleUsers = useMemo(() => {
    const query = userSearch.trim().toLowerCase();
    return users.filter((user) => {
      const assigned = membershipsByUserId.get(user.id) || [];
      const matchesSearch =
        query.length === 0 ||
        user.email.toLowerCase().includes(query) ||
        assigned.some((membership) => membership.project_name.toLowerCase().includes(query));
      const matchesProjectFilter =
        assignedProjectFilter === "all" || assigned.some((membership) => membership.project_id === assignedProjectFilter);
      return matchesSearch && matchesProjectFilter;
    });
  }, [users, membershipsByUserId, userSearch, assignedProjectFilter]);

  function assignmentKey(membership: Membership): string {
    return `${membership.project_id}:${membership.user_id}`;
  }

  function availableProjectsForUser(userId: string): Project[] {
    const assigned = membershipsByUserId.get(userId) || [];
    const assignedSet = new Set(assigned.map((membership) => membership.project_id));
    return projects.filter((project) => !assignedSet.has(project.id));
  }

  async function loadUsers() {
    setUsersLoading(true);
    try {
      const query = new URLSearchParams({ limit: "500" });
      if (userSearch.trim()) query.set("search", userSearch.trim());
      if (userActiveFilter !== "all") query.set("is_active", userActiveFilter);
      if (userApprovalFilter !== "all") query.set("is_approved", userApprovalFilter);
      if (userSysadminFilter !== "all") query.set("is_sysadmin", userSysadminFilter);
      const usersData = await apiFetch(`/users?${query.toString()}`);
      setUsers((usersData?.items || []) as UserRow[]);
    } finally {
      setUsersLoading(false);
    }
  }

  async function loadProjects() {
    const projectsData = await apiFetch("/settings/projects");
    setProjects((projectsData || []) as Project[]);
  }

  async function loadMemberships() {
    setMembershipsLoading(true);
    try {
      const aggregated: Membership[] = [];
      let cursor: string | null = null;
      let guard = 0;
      do {
        const query = new URLSearchParams({ limit: "500" });
        if (cursor) query.set("cursor", cursor);
        const data = await apiFetch(`/settings/rbac/project-memberships?${query.toString()}`);
        const items = (data?.items || []) as Membership[];
        aggregated.push(...items);
        cursor = (data?.next_cursor as string | null) || null;
        guard += 1;
      } while (cursor && guard < 20);
      setMemberships(aggregated);
    } finally {
      setMembershipsLoading(false);
    }
  }

  useEffect(() => {
    loadUsers().catch((err) => setError(err instanceof Error ? err.message : "Failed to load identities"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSearch, userActiveFilter, userApprovalFilter, userSysadminFilter]);

  useEffect(() => {
    Promise.all([loadProjects(), loadMemberships()]).catch((err) =>
      setError(err instanceof Error ? err.message : "Failed to load project assignments"),
    );
  }, []);

  useEffect(() => {
    for (const user of users) {
      if (!rowRoleDraft[user.id]) {
        setRowRoleDraft((prev) => ({ ...prev, [user.id]: "viewer" }));
      }
      if (!rowAllProjectsRoleDraft[user.id]) {
        setRowAllProjectsRoleDraft((prev) => ({ ...prev, [user.id]: "viewer" }));
      }
      const available = availableProjectsForUser(user.id);
      if (!rowProjectDraft[user.id] && available.length > 0) {
        setRowProjectDraft((prev) => ({ ...prev, [user.id]: available[0].id }));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [users, projects, memberships]);

  async function refreshAllAssignmentsAndUsers() {
    await Promise.all([loadUsers(), loadMemberships()]);
  }

  async function createUser(event: FormEvent<HTMLFormElement>) {
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
      setInfo("Identity created and IAM policy applied.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(true);
      setNewUserAllProjects(false);
      setNewUserAllProjectsRole("viewer");
      await refreshAllAssignmentsAndUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create identity");
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
      setError(err instanceof Error ? err.message : "Failed to update identity");
    }
  }

  async function resetPassword(user: UserRow) {
    const nextPassword = window.prompt(`Set temporary password for ${user.email} (minimum 12 characters):`);
    if (!nextPassword) return;
    if (nextPassword.length < 12) {
      setError("Temporary password must be at least 12 characters.");
      return;
    }
    await patchUser(user.id, { password: nextPassword }, "Password reset complete.");
  }

  async function upsertMembership(userId: string, projectId: string, role: string, successMessage: string) {
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/settings/rbac/project-memberships", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, user_id: userId, role }),
      });
      setInfo(successMessage);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update project assignment");
    }
  }

  async function removeMembership(membership: Membership) {
    if (!window.confirm(`Remove ${membership.user_email} from ${membership.project_name}?`)) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/rbac/project-memberships/${membership.project_id}/${membership.user_id}`, { method: "DELETE" });
      setInfo("Project assignment removed.");
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove project assignment");
    }
  }

  async function assignAllProjectsForUser(user: UserRow) {
    const role = rowAllProjectsRoleDraft[user.id] || "viewer";
    const overwrite = !!rowAllProjectsOverwrite[user.id];
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/settings/rbac/users/${user.id}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role, overwrite_existing: overwrite }),
      });
      const updated = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      setInfo(`Applied all-project access policy for ${user.email}: ${updated} membership(s) updated.`);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply all-project policy");
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

      <div className="workspace-section grid gap-4 xl:grid-cols-3">
        <section className="workspace-card">
          <h2 className="text-lg font-semibold">System Role Model</h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Who can administer platform-level IAM controls.</p>
          <ul className="mt-3 space-y-2 text-sm">
            <li>
              <span className="rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200">
                System Admin
              </span>
              <p className="mt-1 text-xs text-slate-500">Can manage identities, assignments, API tokens, and global governance logs.</p>
            </li>
            <li>
              <span className="rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                Standard User
              </span>
              <p className="mt-1 text-xs text-slate-500">No system admin permissions. Access comes from project role assignments.</p>
            </li>
          </ul>
        </section>

        <section className="workspace-card">
          <h2 className="text-lg font-semibold">Project Role Model</h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">What each identity can do inside assigned projects.</p>
          <ul className="mt-3 space-y-2 text-sm">
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("viewer")}`}>viewer</span>
              <p className="mt-1 text-xs text-slate-500">Read-only project visibility.</p>
            </li>
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("operator")}`}>operator</span>
              <p className="mt-1 text-xs text-slate-500">Operational workflows without full project administration.</p>
            </li>
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("admin")}`}>admin</span>
              <p className="mt-1 text-xs text-slate-500">Full project administration including membership governance.</p>
            </li>
          </ul>
        </section>

        <section className="workspace-card">
          <h2 className="text-lg font-semibold">IAM Snapshot</h2>
          <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
            <div className="rounded border border-slate-300 px-2 py-2 dark:border-slate-700">
              <p className="text-xs text-slate-500">Identities</p>
              <p className="font-semibold">{userStats.total}</p>
            </div>
            <div className="rounded border border-slate-300 px-2 py-2 dark:border-slate-700">
              <p className="text-xs text-slate-500">System Admins</p>
              <p className="font-semibold">{userStats.sysadmins}</p>
            </div>
            <div className="rounded border border-slate-300 px-2 py-2 dark:border-slate-700">
              <p className="text-xs text-slate-500">Active</p>
              <p className="font-semibold">{userStats.active}</p>
            </div>
            <div className="rounded border border-slate-300 px-2 py-2 dark:border-slate-700">
              <p className="text-xs text-slate-500">Pending</p>
              <p className="font-semibold">{userStats.pending}</p>
            </div>
          </div>
          <p className="mt-3 text-xs text-amber-700">
            Safety controls prevent self-lockout and prevent removing the final admin from system or project.
          </p>
        </section>
      </div>

      <div className="workspace-section grid gap-4 xl:grid-cols-[420px_1fr]">
        <section className="workspace-card">
          <h2 className="text-lg font-semibold">Create Identity</h2>
          <form className="mt-3 space-y-3" onSubmit={createUser}>
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
              placeholder="Temporary password (12+ chars)"
              type="password"
              value={newUserPassword}
              onChange={(event) => setNewUserPassword(event.target.value)}
              minLength={12}
              required
            />
            <label className="flex items-center gap-2 text-sm">
              <input checked={newUserSysadmin} onChange={(event) => setNewUserSysadmin(event.target.checked)} type="checkbox" />
              Assign System Admin role
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input checked={newUserApproved} onChange={(event) => setNewUserApproved(event.target.checked)} type="checkbox" />
              Mark approved on create
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input checked={newUserAllProjects} onChange={(event) => setNewUserAllProjects(event.target.checked)} type="checkbox" />
              Add to all projects now
            </label>
            {newUserAllProjects ? (
              <label className="block text-sm">
                Baseline project role
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
              Create identity
            </button>
          </form>
        </section>

        <section className="workspace-card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Identity and Access Matrix</h2>
            <div className="text-xs text-slate-500">Single-table IAM view for identity state and project assignments.</div>
          </div>

          <div className="mt-3 grid gap-2 md:grid-cols-5">
            <input
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search email or project"
              value={userSearch}
              onChange={(event) => setUserSearch(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={userActiveFilter}
              onChange={(event) => setUserActiveFilter(event.target.value)}
            >
              <option value="all">All activity</option>
              <option value="true">Active only</option>
              <option value="false">Disabled only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={userApprovalFilter}
              onChange={(event) => setUserApprovalFilter(event.target.value)}
            >
              <option value="all">All approval</option>
              <option value="true">Approved only</option>
              <option value="false">Pending only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={userSysadminFilter}
              onChange={(event) => setUserSysadminFilter(event.target.value)}
            >
              <option value="all">All system roles</option>
              <option value="true">System Admin only</option>
              <option value="false">Standard User only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={assignedProjectFilter}
              onChange={(event) => setAssignedProjectFilter(event.target.value)}
            >
              <option value="all">All assigned projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>

          {usersLoading || membershipsLoading ? <p className="mt-3 text-sm text-slate-500">Loading IAM matrix…</p> : null}
          <div className="mt-3 overflow-auto">
            <table className="data-table min-w-[1450px]">
              <thead>
                <tr>
                  <th>Identity</th>
                  <th>System Role</th>
                  <th>Status</th>
                  <th>Project Assignments</th>
                  <th>Assignment Actions</th>
                  <th>Identity Actions</th>
                </tr>
              </thead>
              <tbody>
                {visibleUsers.length === 0 ? (
                  <tr>
                    <td className="text-sm text-slate-500" colSpan={6}>
                      No identities found.
                    </td>
                  </tr>
                ) : (
                  visibleUsers.map((user) => {
                    const assigned = membershipsByUserId.get(user.id) || [];
                    const available = availableProjectsForUser(user.id);
                    const addProjectId = rowProjectDraft[user.id] || (available[0]?.id ?? "");
                    const addRole = rowRoleDraft[user.id] || "viewer";
                    const allRole = rowAllProjectsRoleDraft[user.id] || "viewer";
                    const allOverwrite = !!rowAllProjectsOverwrite[user.id];
                    return (
                      <tr key={user.id}>
                        <td>
                          <div className="font-semibold">{user.email}</div>
                          <div className="text-xs text-slate-500">Created: {new Date(user.created_at).toLocaleString()}</div>
                        </td>
                        <td>
                          <span
                            className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${
                              user.is_sysadmin
                                ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200"
                                : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200"
                            }`}
                          >
                            {user.is_sysadmin ? "System Admin" : "Standard User"}
                          </span>
                        </td>
                        <td className="text-xs">
                          <div>{user.is_active ? "active" : "disabled"}</div>
                          <div>{user.is_approved ? "approved" : "pending approval"}</div>
                          {user.id === me.id ? <div className="mt-1 text-amber-700">self-protection enabled</div> : null}
                        </td>
                        <td>
                          <div className="max-w-[420px] space-y-2 text-xs">
                            {assigned.length === 0 ? <p className="text-slate-500">No project assignments.</p> : null}
                            {assigned.map((membership) => {
                              const key = assignmentKey(membership);
                              const draftRole = membershipRoleDraft[key] || membership.role;
                              const changed = draftRole !== membership.role;
                              return (
                                <div className="rounded border border-slate-200 p-2 dark:border-slate-700" key={key}>
                                  <div className="mb-2 flex items-center justify-between gap-2">
                                    <span className="font-semibold">{membership.project_name}</span>
                                    <span className={`rounded px-2 py-1 text-[10px] font-semibold uppercase tracking-wide ${rolePillClass(membership.role)}`}>
                                      {membership.role}
                                    </span>
                                  </div>
                                  <div className="flex flex-wrap items-center gap-2">
                                    <select
                                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                                      value={draftRole}
                                      onChange={(event) =>
                                        setMembershipRoleDraft((prev) => ({ ...prev, [key]: event.target.value }))
                                      }
                                    >
                                      {PROJECT_ROLES.map((role) => (
                                        <option key={role} value={role}>
                                          {role}
                                        </option>
                                      ))}
                                    </select>
                                    <button
                                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
                                      disabled={!changed}
                                      onClick={() =>
                                        upsertMembership(
                                          membership.user_id,
                                          membership.project_id,
                                          draftRole,
                                          `Updated ${membership.project_name} assignment for ${membership.user_email}.`,
                                        )
                                      }
                                      type="button"
                                    >
                                      Update
                                    </button>
                                    <button
                                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                                      onClick={() => removeMembership(membership)}
                                      type="button"
                                    >
                                      Remove
                                    </button>
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        </td>
                        <td>
                          <div className="max-w-[280px] space-y-3 text-xs">
                            <div className="rounded border border-slate-200 p-2 dark:border-slate-700">
                              <p className="mb-1 font-semibold">Add project assignment</p>
                              <div className="space-y-1">
                                <select
                                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                                  value={addProjectId}
                                  onChange={(event) =>
                                    setRowProjectDraft((prev) => ({ ...prev, [user.id]: event.target.value }))
                                  }
                                  disabled={available.length === 0}
                                >
                                  {available.length === 0 ? <option value="">No unassigned projects</option> : null}
                                  {available.map((project) => (
                                    <option key={project.id} value={project.id}>
                                      {project.name}
                                    </option>
                                  ))}
                                </select>
                                <select
                                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                                  value={addRole}
                                  onChange={(event) =>
                                    setRowRoleDraft((prev) => ({ ...prev, [user.id]: event.target.value }))
                                  }
                                >
                                  {PROJECT_ROLES.map((role) => (
                                    <option key={role} value={role}>
                                      {role}
                                    </option>
                                  ))}
                                </select>
                                <button
                                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
                                  disabled={!addProjectId}
                                  onClick={() =>
                                    upsertMembership(
                                      user.id,
                                      addProjectId,
                                      addRole,
                                      `Added ${user.email} to selected project.`,
                                    )
                                  }
                                  type="button"
                                >
                                  Add assignment
                                </button>
                              </div>
                            </div>

                            <div className="rounded border border-slate-200 p-2 dark:border-slate-700">
                              <p className="mb-1 font-semibold">Apply all-project baseline</p>
                              <div className="space-y-1">
                                <select
                                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                                  value={allRole}
                                  onChange={(event) =>
                                    setRowAllProjectsRoleDraft((prev) => ({ ...prev, [user.id]: event.target.value }))
                                  }
                                >
                                  {PROJECT_ROLES.map((role) => (
                                    <option key={role} value={role}>
                                      {role}
                                    </option>
                                  ))}
                                </select>
                                <label className="flex items-center gap-1">
                                  <input
                                    checked={allOverwrite}
                                    onChange={(event) =>
                                      setRowAllProjectsOverwrite((prev) => ({ ...prev, [user.id]: event.target.checked }))
                                    }
                                    type="checkbox"
                                  />
                                  Overwrite existing assignments
                                </label>
                                <button
                                  className="w-full rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                                  onClick={() => assignAllProjectsForUser(user)}
                                  type="button"
                                >
                                  Apply all projects
                                </button>
                              </div>
                            </div>
                          </div>
                        </td>
                        <td>
                          <div className="max-w-[210px] space-y-1 text-xs">
                            <button
                              className="w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 disabled:opacity-50"
                              disabled={user.id === me.id}
                              onClick={() =>
                                patchUser(
                                  user.id,
                                  { is_active: !user.is_active },
                                  user.is_active ? `Disabled ${user.email}.` : `Enabled ${user.email}.`,
                                )
                              }
                              type="button"
                            >
                              {user.is_active ? "Disable" : "Enable"}
                            </button>
                            <button
                              className="w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 disabled:opacity-50"
                              disabled={user.id === me.id}
                              onClick={() =>
                                patchUser(
                                  user.id,
                                  { is_approved: !user.is_approved },
                                  user.is_approved ? `Unapproved ${user.email}.` : `Approved ${user.email}.`,
                                )
                              }
                              type="button"
                            >
                              {user.is_approved ? "Unapprove" : "Approve"}
                            </button>
                            <button
                              className="w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 disabled:opacity-50"
                              disabled={user.id === me.id}
                              onClick={() =>
                                patchUser(
                                  user.id,
                                  { is_sysadmin: !user.is_sysadmin },
                                  user.is_sysadmin
                                    ? `Removed System Admin from ${user.email}.`
                                    : `Granted System Admin to ${user.email}.`,
                                )
                              }
                              type="button"
                            >
                              {user.is_sysadmin ? "Demote" : "Promote"}
                            </button>
                            <button
                              className="w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700"
                              onClick={() => resetPassword(user)}
                              type="button"
                            >
                              Reset Password
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </>
  );
}
