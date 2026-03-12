import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { Membership, PROJECT_ROLES, Project, rolePillClass, UserRow } from "@/lib/iam";

function statusPillClass(state: "positive" | "warning" | "neutral"): string {
  if (state === "positive") return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200";
  if (state === "warning") return "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString();
}

type DirectorySummary = {
  users: {
    total: number;
    active: number;
    pending: number;
    sysadmins: number;
  };
  projects: {
    total: number;
  };
};

export function SettingsIamPage() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [summary, setSummary] = useState<DirectorySummary | null>(null);
  const [loading, setLoading] = useState(true);

  const [userSearch, setUserSearch] = useState("");
  const [userActiveFilter, setUserActiveFilter] = useState("all");
  const [userApprovalFilter, setUserApprovalFilter] = useState("all");
  const [userSysadminFilter, setUserSysadminFilter] = useState("all");
  const [assignedProjectFilter, setAssignedProjectFilter] = useState("all");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(true);
  const [newUserAllProjects, setNewUserAllProjects] = useState(false);
  const [newUserAllProjectsRole, setNewUserAllProjectsRole] = useState("viewer");

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const loadProjects = async () => {
    const data = await apiFetch("/settings/projects");
    setProjects((data || []) as Project[]);
  };

  const loadSummary = async () => {
    const data = await apiFetch("/settings/overview");
    setSummary((data || null) as DirectorySummary | null);
  };

  const loadUsersPage = async () => {
    const query = new URLSearchParams({ limit: "24" });
    if (userSearch.trim()) query.set("search", userSearch.trim());
    if (userActiveFilter !== "all") query.set("is_active", userActiveFilter);
    if (userApprovalFilter !== "all") query.set("is_approved", userApprovalFilter);
    if (userSysadminFilter !== "all") query.set("is_sysadmin", userSysadminFilter);
    if (assignedProjectFilter !== "all") query.set("project_id", assignedProjectFilter);
    if (cursor) query.set("cursor", cursor);

    const data = await apiFetch(`/users?${query.toString()}`);
    const rows = ((data?.items || []) as UserRow[]) || [];
    setUsers(rows);
    setNextCursor((data?.next_cursor as string | null) || null);
    return rows;
  };

  const loadMembershipsForUsers = async (userIds: string[]) => {
    if (userIds.length === 0) {
      setMemberships([]);
      return;
    }
    const rows = await apiFetchAllPages<Membership>((pageCursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (pageCursor) query.set("cursor", pageCursor);
      for (const userId of userIds) {
        query.append("user_ids", userId);
      }
      return `/settings/rbac/project-memberships?${query.toString()}`;
    });
    setMemberships(rows);
  };

  const refreshDirectory = async (includeMetadata = false) => {
    setLoading(true);
    setError(null);
    try {
      if (includeMetadata) {
        await Promise.all([loadProjects(), loadSummary()]);
      }
      const rows = await loadUsersPage();
      await loadMembershipsForUsers(rows.map((user) => user.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load IAM directory");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    Promise.all([loadProjects(), loadSummary()]).catch((err) => {
      setError(err instanceof Error ? err.message : "Failed to load IAM metadata");
    });
  }, []);

  useEffect(() => {
    refreshDirectory().catch(() => undefined);
  }, [assignedProjectFilter, cursor, userActiveFilter, userApprovalFilter, userSearch, userSysadminFilter]);

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

  useEffect(() => {
    setCursor(null);
    setHistory([]);
  }, [assignedProjectFilter, userActiveFilter, userApprovalFilter, userSearch, userSysadminFilter]);

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
      setInfo("Identity created.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(true);
      setNewUserAllProjects(false);
      setNewUserAllProjectsRole("viewer");
      await Promise.all([loadSummary(), refreshDirectory()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create identity");
    }
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

      <div className="workspace-section grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Identities</p>
          <p className="mt-2 text-3xl font-semibold">{summary?.users.total ?? users.length}</p>
          <p className="mt-2 text-sm text-slate-500">Directory-wide user count.</p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">System Admins</p>
          <p className="mt-2 text-3xl font-semibold">{summary?.users.sysadmins ?? 0}</p>
          <p className="mt-2 text-sm text-slate-500">Platform-wide administrators.</p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Pending Approval</p>
          <p className="mt-2 text-3xl font-semibold">{summary?.users.pending ?? 0}</p>
          <p className="mt-2 text-sm text-slate-500">Accounts waiting for approval.</p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Projects</p>
          <p className="mt-2 text-3xl font-semibold">{summary?.projects.total ?? projects.length}</p>
          <p className="mt-2 text-sm text-slate-500">Projects currently represented in the access catalog.</p>
        </section>
      </div>

      <div className="workspace-section grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <section className="workspace-card">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Create Identity</p>
            <h2 className="mt-2 text-xl font-semibold">New user</h2>
            <p className="mt-1 text-sm text-slate-500">Create the account here. Project-specific changes happen on the user detail page.</p>
          </div>

          <form className="mt-5 space-y-3" onSubmit={createUser}>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
              Email
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                placeholder="user@example.com"
                type="email"
                value={newUserEmail}
                onChange={(event) => setNewUserEmail(event.target.value)}
                required
              />
            </label>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
              Temporary password
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                placeholder="Must satisfy server password policy"
                type="password"
                value={newUserPassword}
                onChange={(event) => setNewUserPassword(event.target.value)}
                required
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input checked={newUserSysadmin} onChange={(event) => setNewUserSysadmin(event.target.checked)} type="checkbox" />
              Grant system admin
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input checked={newUserApproved} onChange={(event) => setNewUserApproved(event.target.checked)} type="checkbox" />
              Mark approved immediately
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
              <input checked={newUserAllProjects} onChange={(event) => setNewUserAllProjects(event.target.checked)} type="checkbox" />
              Add to every project on create
            </label>
            {newUserAllProjects ? (
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                Baseline project role
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
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
            <button className="w-full rounded-lg bg-pine px-3 py-2 text-sm font-semibold text-white" type="submit">
              Create identity
            </button>
          </form>
        </section>

        <section className="workspace-card space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">IAM Directory</p>
              <h2 className="mt-2 text-xl font-semibold">Users and access tags</h2>
              <p className="mt-1 text-sm text-slate-500">Each card shows identity state plus project role tags. Use Manage to edit one user at a time.</p>
            </div>
            <button
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
              onClick={() => refreshDirectory().catch(() => undefined)}
              type="button"
            >
              Refresh
            </button>
          </div>

          <div className="grid gap-2 md:grid-cols-5">
            <input
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search email or project"
              value={userSearch}
              onChange={(event) => setUserSearch(event.target.value)}
            />
            <select
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={userActiveFilter}
              onChange={(event) => setUserActiveFilter(event.target.value)}
            >
              <option value="all">All activity</option>
              <option value="true">Active only</option>
              <option value="false">Disabled only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={userApprovalFilter}
              onChange={(event) => setUserApprovalFilter(event.target.value)}
            >
              <option value="all">All approval</option>
              <option value="true">Approved only</option>
              <option value="false">Pending only</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={userSysadminFilter}
              onChange={(event) => setUserSysadminFilter(event.target.value)}
            >
              <option value="all">All system roles</option>
              <option value="true">System admins</option>
              <option value="false">Standard users</option>
            </select>
            <select
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={assignedProjectFilter}
              onChange={(event) => setAssignedProjectFilter(event.target.value)}
            >
              <option value="all">All projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>

          {loading ? <p className="text-sm text-slate-500">Loading IAM directory…</p> : null}

          {!loading && users.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-slate-300 px-4 py-8 text-center text-sm text-slate-500 dark:border-slate-700">
              No identities match the current filters.
            </div>
          ) : null}

          <div className="grid gap-3 2xl:grid-cols-2">
            {users.map((user) => {
              const assigned = membershipsByUserId.get(user.id) || [];
              const visibleTags = assigned.slice(0, 4);
              const hiddenCount = Math.max(0, assigned.length - visibleTags.length);
              return (
                <article className="rounded-2xl border border-slate-200 p-4 dark:border-slate-800" key={user.id}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="text-base font-semibold">{user.email}</h3>
                      <p className="mt-1 text-xs text-slate-500">Created {formatDate(user.created_at)}</p>
                    </div>
                    <Link
                      className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                      to={`/settings/iam/users/${user.id}`}
                    >
                      Manage
                    </Link>
                  </div>

                  <div className="mt-4 flex flex-wrap gap-2 text-xs font-semibold uppercase tracking-wide">
                    <span className={`rounded-full px-2.5 py-1 ${user.is_sysadmin ? statusPillClass("positive") : statusPillClass("neutral")}`}>
                      {user.is_sysadmin ? "System admin" : "Standard user"}
                    </span>
                    <span className={`rounded-full px-2.5 py-1 ${user.is_active ? statusPillClass("positive") : statusPillClass("warning")}`}>
                      {user.is_active ? "Active" : "Disabled"}
                    </span>
                    <span className={`rounded-full px-2.5 py-1 ${user.is_approved ? statusPillClass("positive") : statusPillClass("warning")}`}>
                      {user.is_approved ? "Approved" : "Pending approval"}
                    </span>
                    <span className={`rounded-full px-2.5 py-1 ${statusPillClass("neutral")}`}>{assigned.length} project{assigned.length === 1 ? "" : "s"}</span>
                  </div>

                  <div className="mt-4 rounded-2xl bg-slate-50 p-3 dark:bg-slate-900/60">
                    <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Project access</p>
                    {assigned.length === 0 ? (
                      <p className="mt-2 text-sm text-slate-500">No project assignments.</p>
                    ) : (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {visibleTags.map((membership) => (
                          <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-700 dark:border-slate-700 dark:text-slate-200" key={`${membership.project_id}:${membership.user_id}`}>
                            <span>{membership.project_name}</span>
                            <span className={`rounded-full px-2 py-0.5 font-semibold uppercase tracking-wide ${rolePillClass(membership.role)}`}>
                              {membership.role}
                            </span>
                          </span>
                        ))}
                        {hiddenCount > 0 ? (
                          <span className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-300">
                            +{hiddenCount} more
                          </span>
                        ) : null}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--app-border)] pt-3">
            <p className="text-xs text-slate-500">Membership tags are loaded only for users on the current page.</p>
            <div className="flex gap-2">
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                disabled={history.length === 0}
                onClick={previousPage}
                type="button"
              >
                Previous
              </button>
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                disabled={!nextCursor}
                onClick={nextPage}
                type="button"
              >
                Next
              </button>
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
