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

  const [membershipSearchDraft, setMembershipSearchDraft] = useState("");
  const [membershipSearch, setMembershipSearch] = useState("");
  const [membershipCursor, setMembershipCursor] = useState<string | null>(null);
  const [membershipHistory, setMembershipHistory] = useState<Array<string | null>>([]);
  const [membershipNextCursor, setMembershipNextCursor] = useState<string | null>(null);

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(true);
  const [newUserAllProjects, setNewUserAllProjects] = useState(false);
  const [newUserAllProjectsRole, setNewUserAllProjectsRole] = useState("viewer");

  const [assignTargetUserId, setAssignTargetUserId] = useState("");
  const [assignTargetProjectId, setAssignTargetProjectId] = useState("");
  const [assignTargetRole, setAssignTargetRole] = useState("viewer");

  const [assignAllUserId, setAssignAllUserId] = useState("");
  const [assignAllRole, setAssignAllRole] = useState("viewer");
  const [assignAllOverwrite, setAssignAllOverwrite] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const userStats = useMemo(() => {
    const total = users.length;
    const active = users.filter((user) => user.is_active).length;
    const pending = users.filter((user) => !user.is_approved).length;
    const sysadmins = users.filter((user) => user.is_sysadmin).length;
    return { total, active, pending, sysadmins };
  }, [users]);

  async function loadUsers() {
    setUsersLoading(true);
    try {
      const query = new URLSearchParams({ limit: "500" });
      if (userSearch.trim()) query.set("search", userSearch.trim());
      if (userActiveFilter !== "all") query.set("is_active", userActiveFilter);
      if (userApprovalFilter !== "all") query.set("is_approved", userApprovalFilter);
      if (userSysadminFilter !== "all") query.set("is_sysadmin", userSysadminFilter);
      const usersData = await apiFetch(`/users?${query.toString()}`);
      const rows = (usersData?.items || []) as UserRow[];
      setUsers(rows);

      if (!assignTargetUserId && rows.length > 0) setAssignTargetUserId(rows[0].id);
      if (!assignAllUserId && rows.length > 0) setAssignAllUserId(rows[0].id);
    } finally {
      setUsersLoading(false);
    }
  }

  async function loadProjects() {
    const projectsData = await apiFetch("/settings/projects");
    const rows = (projectsData || []) as Project[];
    setProjects(rows);
    if (!assignTargetProjectId && rows.length > 0) setAssignTargetProjectId(rows[0].id);
  }

  async function loadMemberships() {
    setMembershipsLoading(true);
    try {
      const query = new URLSearchParams({ limit: "200" });
      if (membershipSearch.trim()) query.set("q", membershipSearch.trim());
      if (membershipCursor) query.set("cursor", membershipCursor);
      const data = await apiFetch(`/settings/rbac/project-memberships?${query.toString()}`);
      setMemberships((data?.items || []) as Membership[]);
      setMembershipNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setMembershipsLoading(false);
    }
  }

  useEffect(() => {
    Promise.all([loadUsers(), loadProjects()]).catch((err) => setError(err instanceof Error ? err.message : "Failed to load IAM users/projects"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSearch, userActiveFilter, userApprovalFilter, userSysadminFilter]);

  useEffect(() => {
    loadMemberships().catch((err) => setError(err instanceof Error ? err.message : "Failed to load IAM memberships"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [membershipSearch, membershipCursor]);

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
      setInfo("User created and IAM defaults applied.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(true);
      setNewUserAllProjects(false);
      setNewUserAllProjectsRole("viewer");
      await loadUsers();
      await loadMemberships();
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
      setError(err instanceof Error ? err.message : "Failed to update user");
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

  async function upsertMembership(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!assignTargetUserId || !assignTargetProjectId) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/settings/rbac/project-memberships", {
        method: "POST",
        body: JSON.stringify({
          project_id: assignTargetProjectId,
          user_id: assignTargetUserId,
          role: assignTargetRole,
        }),
      });
      setInfo("Project access assignment updated.");
      setMembershipCursor(null);
      setMembershipHistory([]);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update project access");
    }
  }

  async function removeMembership(membership: Membership) {
    if (!window.confirm(`Remove ${membership.user_email} from ${membership.project_name}?`)) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/rbac/project-memberships/${membership.project_id}/${membership.user_id}`, { method: "DELETE" });
      setInfo("Project access removed.");
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove project access");
    }
  }

  async function assignUserToAllProjects() {
    if (!assignAllUserId) return;
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/settings/rbac/users/${assignAllUserId}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role: assignAllRole, overwrite_existing: assignAllOverwrite }),
      });
      const updated = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      setInfo(`Applied IAM access policy to ${updated} project membership(s).`);
      setMembershipCursor(null);
      setMembershipHistory([]);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply all-project IAM assignment");
    }
  }

  function previousMembershipPage() {
    if (membershipHistory.length === 0) return;
    const copy = [...membershipHistory];
    const previous = copy.pop() ?? null;
    setMembershipHistory(copy);
    setMembershipCursor(previous);
  }

  function nextMembershipPage() {
    if (!membershipNextCursor) return;
    setMembershipHistory((prev) => [...prev, membershipCursor]);
    setMembershipCursor(membershipNextCursor);
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
          <h2 className="text-lg font-semibold">System Roles</h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Identity-level access to administration capabilities.</p>
          <ul className="mt-3 space-y-2 text-sm">
            <li>
              <span className="rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200">
                System Admin
              </span>
              <p className="mt-1 text-xs text-slate-500">Can manage users, IAM policies, API tokens, and global audit visibility.</p>
            </li>
            <li>
              <span className="rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                Standard User
              </span>
              <p className="mt-1 text-xs text-slate-500">No system-wide admin privileges. Access comes from project roles and token scopes.</p>
            </li>
          </ul>
        </section>

        <section className="workspace-card">
          <h2 className="text-lg font-semibold">Project Roles</h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Resource-level permissions inside each project membership.</p>
          <ul className="mt-3 space-y-2 text-sm">
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("viewer")}`}>viewer</span>
              <p className="mt-1 text-xs text-slate-500">Read-only project visibility.</p>
            </li>
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("operator")}`}>operator</span>
              <p className="mt-1 text-xs text-slate-500">Operational workflows (for example imports/runs) without full IAM control.</p>
            </li>
            <li>
              <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass("admin")}`}>admin</span>
              <p className="mt-1 text-xs text-slate-500">Project governance including membership and project-level administration.</p>
            </li>
          </ul>
        </section>

        <section className="workspace-card">
          <h2 className="text-lg font-semibold">IAM Status</h2>
          <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
            <div className="rounded border border-slate-300 px-2 py-2 dark:border-slate-700">
              <p className="text-xs text-slate-500">Users</p>
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
              <p className="text-xs text-slate-500">Pending Approval</p>
              <p className="font-semibold">{userStats.pending}</p>
            </div>
          </div>
          <p className="mt-3 text-xs text-amber-700">
            Safety policy prevents self-lockout and prevents removing the final admin from system or project.
          </p>
        </section>
      </div>

      <div className="workspace-section grid gap-4 xl:grid-cols-3">
        <section className="workspace-card xl:col-span-1">
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
                Global project role
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

        <section className="workspace-card xl:col-span-2">
          <h2 className="text-lg font-semibold">Identity Directory</h2>
          <div className="mt-3 grid gap-2 lg:grid-cols-4">
            <input
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search email"
              value={userSearch}
              onChange={(event) => setUserSearch(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={userActiveFilter}
              onChange={(event) => setUserActiveFilter(event.target.value)}
            >
              <option value="all">All activity states</option>
              <option value="true">Active only</option>
              <option value="false">Disabled only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
              value={userApprovalFilter}
              onChange={(event) => setUserApprovalFilter(event.target.value)}
            >
              <option value="all">All approval states</option>
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
          </div>

          {usersLoading ? <p className="mt-3 text-sm text-slate-500">Loading identities…</p> : null}
          <ul className="mt-3 max-h-[480px] space-y-2 overflow-auto text-sm">
            {users.map((user) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={user.id}>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-semibold">{user.email}</p>
                    <p className="text-xs text-slate-500">
                      {user.is_sysadmin ? "System Admin" : "Standard User"} | {user.is_active ? "active" : "disabled"} |{" "}
                      {user.is_approved ? "approved" : "pending approval"}
                    </p>
                    <p className="text-xs text-slate-500">Created: {new Date(user.created_at).toLocaleString()}</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      disabled={user.id === me.id}
                      onClick={() => patchUser(user.id, { is_active: !user.is_active }, user.is_active ? "Identity disabled." : "Identity enabled.")}
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
                          user.is_approved ? "Approval revoked." : "Identity approved.",
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
                          user.is_sysadmin ? "System Admin role removed." : "System Admin role granted.",
                        )
                      }
                    >
                      {user.is_sysadmin ? "Demote" : "Promote"}
                    </button>
                    <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onClick={() => resetPassword(user)}>
                      Reset Password
                    </button>
                  </div>
                </div>
                {user.id === me.id ? <p className="mt-2 text-xs text-amber-700">Self-protection blocks lockout actions on your own identity.</p> : null}
              </li>
            ))}
          </ul>
        </section>
      </div>

      <div className="workspace-section grid gap-4 xl:grid-cols-3">
        <section className="workspace-card xl:col-span-1 space-y-3">
          <h2 className="text-lg font-semibold">Project Access Assignment</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">Grant or update project-level role for one identity.</p>
          <form className="space-y-3" onSubmit={upsertMembership}>
            <label className="block text-sm">
              Identity
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={assignTargetUserId}
                onChange={(event) => setAssignTargetUserId(event.target.value)}
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
                value={assignTargetProjectId}
                onChange={(event) => setAssignTargetProjectId(event.target.value)}
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Project role
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={assignTargetRole}
                onChange={(event) => setAssignTargetRole(event.target.value)}
              >
                {PROJECT_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Save project access
            </button>
          </form>
        </section>

        <section className="workspace-card xl:col-span-2 space-y-3">
          <h2 className="text-lg font-semibold">Bulk Access Policy</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">Apply one project-role baseline across all projects for a selected identity.</p>
          <div className="grid gap-2 md:grid-cols-4">
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={assignAllUserId}
              onChange={(event) => setAssignAllUserId(event.target.value)}
            >
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.email}
                </option>
              ))}
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={assignAllRole}
              onChange={(event) => setAssignAllRole(event.target.value)}
            >
              {PROJECT_ROLES.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-2 rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700">
              <input checked={assignAllOverwrite} onChange={(event) => setAssignAllOverwrite(event.target.checked)} type="checkbox" />
              Overwrite existing
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="button" onClick={assignUserToAllProjects}>
              Apply all-project policy
            </button>
          </div>
          <p className="text-xs text-slate-500">Project safety rules protect against removing the last project admin.</p>
        </section>
      </div>

      <div className="workspace-section">
        <section className="workspace-card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Project Access Directory</h2>
            <form
              className="flex items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                setMembershipCursor(null);
                setMembershipHistory([]);
                setMembershipSearch(membershipSearchDraft.trim());
              }}
            >
              <input
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search project or identity"
                value={membershipSearchDraft}
                onChange={(event) => setMembershipSearchDraft(event.target.value)}
              />
              <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" type="submit">
                Search
              </button>
            </form>
          </div>

          {membershipsLoading ? <p className="mt-3 text-sm text-slate-500">Loading access assignments…</p> : null}
          <ul className="mt-3 max-h-[460px] space-y-2 overflow-auto text-sm">
            {memberships.length === 0 ? <li className="text-slate-500">No project access records found.</li> : null}
            {memberships.map((membership) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={`${membership.project_id}:${membership.user_id}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="font-semibold">{membership.user_email}</p>
                    <p className="text-xs text-slate-500">{membership.project_name}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass(membership.role)}`}>
                      {membership.role}
                    </span>
                    <button
                      className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                      onClick={() => removeMembership(membership)}
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>

          <div className="mt-3 flex items-center gap-2">
            <button
              className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
              type="button"
              onClick={previousMembershipPage}
              disabled={membershipHistory.length === 0}
            >
              Previous
            </button>
            <button
              className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 disabled:opacity-50"
              type="button"
              onClick={nextMembershipPage}
              disabled={!membershipNextCursor}
            >
              Next
            </button>
          </div>
        </section>
      </div>
    </>
  );
}
