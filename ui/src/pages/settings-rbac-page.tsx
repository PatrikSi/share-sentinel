import { FormEvent, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";

type Project = { id: string; name: string };
type User = { id: string; email: string };
type Membership = {
  project_id: string;
  project_name: string;
  user_id: string;
  user_email: string;
  role: string;
};

const PROJECT_ROLES = ["viewer", "operator", "admin"];

export function SettingsRbacPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [loading, setLoading] = useState(false);

  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [projectId, setProjectId] = useState("");
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState(PROJECT_ROLES[0]);

  const [bulkUserId, setBulkUserId] = useState("");
  const [bulkRole, setBulkRole] = useState("viewer");
  const [bulkOverwrite, setBulkOverwrite] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadReferenceData() {
    const [projectsData, usersData] = await Promise.all([apiFetch("/settings/projects"), apiFetch("/users?limit=500")]);
    const projectRows = (projectsData || []) as Project[];
    const userRows = ((usersData?.items as User[]) || []).sort((a, b) => a.email.localeCompare(b.email));
    setProjects(projectRows);
    setUsers(userRows);
    if (!projectId && projectRows.length > 0) setProjectId(projectRows[0].id);
    if (!userId && userRows.length > 0) setUserId(userRows[0].id);
    if (!bulkUserId && userRows.length > 0) setBulkUserId(userRows[0].id);
  }

  async function loadMemberships() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "200" });
      if (search.trim()) query.set("q", search.trim());
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/rbac/project-memberships?${query.toString()}`);
      setMemberships((data?.items || []) as Membership[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadReferenceData().catch((err) => setError(err instanceof Error ? err.message : "Failed to load projects and users"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadMemberships().catch((err) => setError(err instanceof Error ? err.message : "Failed to load memberships"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  const selectedProjectName = useMemo(() => projects.find((project) => project.id === projectId)?.name || "unknown", [projects, projectId]);
  const selectedUserEmail = useMemo(() => users.find((user) => user.id === userId)?.email || "unknown", [users, userId]);

  async function saveMembership(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId || !userId) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/settings/rbac/project-memberships", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, user_id: userId, role }),
      });
      setInfo(`Membership updated: ${selectedUserEmail} -> ${selectedProjectName} (${role}).`);
      setCursor(null);
      setHistory([]);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update membership");
    }
  }

  async function assignAllProjects() {
    if (!bulkUserId) return;
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/settings/rbac/users/${bulkUserId}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role: bulkRole, overwrite_existing: bulkOverwrite }),
      });
      const assigned = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      setInfo(`Updated ${assigned} project memberships.`);
      setCursor(null);
      setHistory([]);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to assign all project memberships");
    }
  }

  async function removeMembership(membership: Membership) {
    if (!window.confirm(`Remove ${membership.user_email} from ${membership.project_name}?`)) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/rbac/project-memberships/${membership.project_id}/${membership.user_id}`, { method: "DELETE" });
      setInfo(`Membership removed: ${membership.user_email} from ${membership.project_name}.`);
      await loadMemberships();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove membership");
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

      <div className="workspace-section grid gap-4 xl:grid-cols-3">
        <div className="workspace-card space-y-3 xl:col-span-1">
          <h2 className="text-lg font-semibold">Assign Project Role</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">Use this for targeted membership updates.</p>
          <form className="space-y-3" onSubmit={saveMembership}>
            <label className="block text-sm">
              Project
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                required
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              User
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={userId}
                onChange={(event) => setUserId(event.target.value)}
                required
              >
                {users.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Role
              <select
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={role}
                onChange={(event) => setRole(event.target.value)}
              >
                {PROJECT_ROLES.map((projectRole) => (
                  <option key={projectRole} value={projectRole}>
                    {projectRole}
                  </option>
                ))}
              </select>
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit">
              Save membership
            </button>
          </form>
        </div>

        <div className="workspace-card space-y-3 xl:col-span-2">
          <h2 className="text-lg font-semibold">Add User To All Projects</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Enterprise shortcut for onboarding. Existing memberships can be preserved or overwritten.
          </p>
          <div className="grid gap-2 md:grid-cols-4">
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={bulkUserId}
              onChange={(event) => setBulkUserId(event.target.value)}
            >
              {users.map((user) => (
                <option key={user.id} value={user.id}>
                  {user.email}
                </option>
              ))}
            </select>
            <select
              className="rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
              value={bulkRole}
              onChange={(event) => setBulkRole(event.target.value)}
            >
              {PROJECT_ROLES.map((projectRole) => (
                <option key={projectRole} value={projectRole}>
                  {projectRole}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-2 rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700">
              <input checked={bulkOverwrite} onChange={(event) => setBulkOverwrite(event.target.checked)} type="checkbox" />
              Overwrite existing roles
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="button" onClick={assignAllProjects}>
              Apply to all projects
            </button>
          </div>
          <p className="text-xs text-slate-500">Project admin safety blocks changes that would remove the last admin from a project.</p>
        </div>
      </div>

      <div className="workspace-section">
        <div className="workspace-card space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">Current Memberships</h2>
            <form className="flex items-center gap-2" onSubmit={onSearch}>
              <input
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                placeholder="Search project or user"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
              />
              <button className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" type="submit">
                Search
              </button>
            </form>
          </div>
          {loading ? <p className="text-sm text-slate-500">Loading memberships…</p> : null}
          <ul className="max-h-[420px] space-y-2 overflow-auto text-sm">
            {memberships.length === 0 ? <li className="text-slate-500">No memberships found.</li> : null}
            {memberships.map((membership) => (
              <li className="rounded-lg border border-slate-300 p-2 dark:border-slate-700" key={`${membership.project_id}:${membership.user_id}`}>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="font-semibold">{membership.user_email}</div>
                    <div className="text-xs text-slate-500">
                      {membership.project_name} | role {membership.role}
                    </div>
                  </div>
                  <button
                    className="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                    onClick={() => removeMembership(membership)}
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
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
      </div>
    </>
  );
}
