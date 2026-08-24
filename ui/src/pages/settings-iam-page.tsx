import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { Membership, PROJECT_ROLES, Project, UserRow } from "@/lib/iam";

type DirectorySummary = {
  users: {
    total: number;
    active: number;
    pending: number;
    sysadmins: number;
  };
};

type SecuritySettings = {
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
};

type FilterState = {
  search: string;
  is_active: string;
  is_approved: string;
  is_sysadmin: string;
  project_id: string;
};

const DEFAULT_FILTERS: FilterState = {
  search: "",
  is_active: "all",
  is_approved: "all",
  is_sysadmin: "all",
  project_id: "all",
};

type DirectoryRequest = {
  id: number;
  filterKey: string;
  userIdsKey: string | null;
  controller: AbortController;
};

type DirectoryPage = {
  rows: UserRow[];
  nextCursor: string | null;
};

type MembershipPreview = {
  rows: Membership[];
  limited: boolean;
};

function userDirectoryPath(activeFilters: FilterState, activeCursor: string | null): string {
  const query = new URLSearchParams({ limit: "30" });
  if (activeFilters.search.trim()) query.set("search", activeFilters.search.trim());
  if (activeFilters.is_active !== "all") query.set("is_active", activeFilters.is_active);
  if (activeFilters.is_approved !== "all") query.set("is_approved", activeFilters.is_approved);
  if (activeFilters.is_sysadmin !== "all") query.set("is_sysadmin", activeFilters.is_sysadmin);
  if (activeFilters.project_id !== "all") query.set("project_id", activeFilters.project_id);
  if (activeCursor) query.set("cursor", activeCursor);
  return `/users?${query.toString()}`;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString();
}

function passwordPolicySummary(settings: SecuritySettings | null): string {
  if (!settings) return "Password policy unavailable.";
  const parts = [`Minimum ${settings.password_min_length} characters`];
  if (settings.password_require_lowercase) parts.push("lowercase");
  if (settings.password_require_uppercase) parts.push("uppercase");
  if (settings.password_require_number) parts.push("number");
  if (settings.password_require_special) parts.push("special character");
  return parts.join(", ");
}

function userStateBadges(user: UserRow): Array<{ label: string; className: string }> {
  return [
    {
      label: user.is_active ? "Active" : "Disabled",
      className: user.is_active ? "settings-badge settings-badge-positive" : "settings-badge settings-badge-warning",
    },
    {
      label: user.is_approved ? "Approved" : "Pending approval",
      className: user.is_approved ? "settings-badge settings-badge-neutral" : "settings-badge settings-badge-warning",
    },
    {
      label: user.is_sysadmin ? "Sysadmin" : "Standard user",
      className: user.is_sysadmin ? "settings-badge settings-badge-positive" : "settings-badge settings-badge-neutral",
    },
  ];
}

export function SettingsIamPage() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [summary, setSummary] = useState<DirectorySummary | null>(null);
  const [securitySettings, setSecuritySettings] = useState<SecuritySettings | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [membershipPreviewLimited, setMembershipPreviewLimited] = useState(false);
  const [membershipPreviewError, setMembershipPreviewError] = useState<string | null>(null);

  const directoryRequestSequence = useRef(0);
  const directoryRequest = useRef<DirectoryRequest | null>(null);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [filterDraft, setFilterDraft] = useState<FilterState>(DEFAULT_FILTERS);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserSysadmin, setNewUserSysadmin] = useState(false);
  const [newUserApproved, setNewUserApproved] = useState(false);
  const [newUserAllProjects, setNewUserAllProjects] = useState(false);
  const [newUserAllProjectsRole, setNewUserAllProjectsRole] = useState("viewer");

  async function loadProjects() {
    const data = await apiFetch("/settings/projects");
    setProjects((data || []) as Project[]);
  }

  async function loadSummary() {
    const data = await apiFetch("/settings/overview");
    setSummary((data || null) as DirectorySummary | null);
  }

  async function loadSecuritySettings() {
    const data = await apiFetch("/auth/security-settings");
    setSecuritySettings(data as SecuritySettings);
  }

  async function loadUsersPage(path: string, signal: AbortSignal): Promise<DirectoryPage> {
    const data = await apiFetch(path, { signal });
    return {
      rows: ((data?.items || []) as UserRow[]) || [],
      nextCursor: (data?.next_cursor as string | null) || null,
    };
  }

  async function loadMembershipsForUsers(userIds: string[], signal: AbortSignal): Promise<MembershipPreview> {
    if (userIds.length === 0) {
      return { rows: [], limited: false };
    }
    const result = await apiFetchAllPages<Membership>((pageCursor) => {
      const query = new URLSearchParams({ limit: "250" });
      if (pageCursor) query.set("cursor", pageCursor);
      for (const userId of userIds) {
        query.append("user_ids", userId);
      }
      return `/settings/rbac/project-memberships?${query.toString()}`;
    }, { signal }, { maxPages: 10, maxItems: 2_500, maxDurationMs: 10_000 });
    return { rows: result.items, limited: result.truncated };
  }

  function isCurrentDirectoryRequest(requestId: number, filterKey: string, userIdsKey?: string): boolean {
    const current = directoryRequest.current;
    if (!current || current.id !== requestId || current.filterKey !== filterKey || current.controller.signal.aborted) {
      return false;
    }
    return userIdsKey === undefined || current.userIdsKey === userIdsKey;
  }

  async function refreshDirectory(activeFilters = filters, activeCursor = cursor) {
    const filterKey = userDirectoryPath(activeFilters, activeCursor);
    const requestId = ++directoryRequestSequence.current;
    const controller = new AbortController();
    directoryRequest.current?.controller.abort();
    directoryRequest.current = { id: requestId, filterKey, userIdsKey: null, controller };

    setLoading(true);
    setError(null);
    setDirectoryError(null);
    setUsers([]);
    setMemberships([]);
    setMembershipPreviewLimited(false);
    setMembershipPreviewError(null);
    setNextCursor(null);
    try {
      const page = await loadUsersPage(filterKey, controller.signal);
      if (!isCurrentDirectoryRequest(requestId, filterKey)) return;

      setUsers(page.rows);
      setNextCursor(page.nextCursor);

      const userIds = page.rows.map((user) => user.id);
      const userIdsKey = JSON.stringify(userIds);
      const current = directoryRequest.current;
      if (!current || current.id !== requestId || current.filterKey !== filterKey) return;
      current.userIdsKey = userIdsKey;

      try {
        const membershipPreview = await loadMembershipsForUsers(userIds, controller.signal);
        if (!isCurrentDirectoryRequest(requestId, filterKey, userIdsKey)) return;
        setMemberships(membershipPreview.rows);
        setMembershipPreviewLimited(membershipPreview.limited);
      } catch (err) {
        if (!isCurrentDirectoryRequest(requestId, filterKey, userIdsKey)) return;
        setMemberships([]);
        setMembershipPreviewLimited(false);
        setMembershipPreviewError(err instanceof Error ? err.message : "Failed to load project access previews");
      }
    } catch (err) {
      if (!isCurrentDirectoryRequest(requestId, filterKey)) return;
      setDirectoryError(err instanceof Error ? err.message : "Failed to load users");
    } finally {
      if (isCurrentDirectoryRequest(requestId, filterKey)) {
        directoryRequest.current = null;
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    Promise.all([loadProjects(), loadSummary(), loadSecuritySettings()]).catch((err) => {
      setError(err instanceof Error ? err.message : "Failed to load user admin metadata");
    });
  }, []);

  useEffect(() => {
    refreshDirectory(filters, cursor).catch(() => undefined);
    const filterKey = userDirectoryPath(filters, cursor);
    return () => {
      const current = directoryRequest.current;
      if (current?.filterKey === filterKey) {
        directoryRequest.current = null;
        current.controller.abort();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, cursor]);

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
      setInfo("User created.");
      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserSysadmin(false);
      setNewUserApproved(false);
      setNewUserAllProjects(false);
      setNewUserAllProjectsRole("viewer");
      setShowCreateForm(false);
      await Promise.all([loadProjects(), loadSummary(), loadSecuritySettings(), refreshDirectory(filters, cursor)]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    }
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setHistory([]);
    setCursor(null);
    setFilters({ ...filterDraft });
  }

  function clearFilters() {
    setHistory([]);
    setCursor(null);
    setFilterDraft(DEFAULT_FILTERS);
    setFilters(DEFAULT_FILTERS);
  }

  function setQuickView(partial: Partial<FilterState>) {
    const nextFilters = { ...DEFAULT_FILTERS, ...partial };
    setHistory([]);
    setCursor(null);
    setFilterDraft(nextFilters);
    setFilters(nextFilters);
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
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <h2 className="settings-page-title">Users</h2>
          <p className="settings-page-copy">Manage user lifecycle, approvals, system administrators, and project access.</p>
        </div>
        <div className="settings-toolbar">
          <button
            className="settings-button"
            onClick={() => {
              setError(null);
              setInfo(null);
              setShowCreateForm((open) => !open);
            }}
            type="button"
          >
            {showCreateForm ? "Close User Form" : "New User"}
          </button>
          <button className="settings-button" onClick={() => refreshDirectory(filters, cursor).catch(() => undefined)} type="button">
            Refresh
          </button>
        </div>
      </div>

      {error ? (
        <div className="settings-panel">
          <p className="text-sm text-rose-700 dark:text-rose-200">{error}</p>
        </div>
      ) : null}
      {info ? (
        <div className="settings-panel">
          <p className="text-sm text-emerald-700 dark:text-emerald-200">{info}</p>
        </div>
      ) : null}

      <section className="settings-panel">
        <div className="settings-grid-3">
          <div className="settings-kpi">
            <span className="settings-kpi-label">Total users</span>
            <span className="settings-kpi-value">{summary?.users.total ?? users.length}</span>
            <p className="settings-kpi-copy">Full directory count.</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Pending approval</span>
            <span className="settings-kpi-value">{summary?.users.pending ?? 0}</span>
            <p className="settings-kpi-copy">Accounts waiting for review.</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Sysadmins</span>
            <span className="settings-kpi-value">{summary?.users.sysadmins ?? 0}</span>
            <p className="settings-kpi-copy">Global administrators.</p>
          </div>
        </div>
      </section>

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Quick Views</h3>
            <p className="settings-panel-copy">Start from the most common user queues and states.</p>
          </div>
        </div>
        <div className="mt-4 settings-toolbar">
          <button className="settings-button" onClick={() => setQuickView(DEFAULT_FILTERS)} type="button">
            All Users
          </button>
          <button className="settings-button" onClick={() => setQuickView({ is_approved: "false" })} type="button">
            Pending Approval
          </button>
          <button className="settings-button" onClick={() => setQuickView({ is_active: "false" })} type="button">
            Disabled
          </button>
          <button className="settings-button" onClick={() => setQuickView({ is_sysadmin: "true" })} type="button">
            Sysadmins
          </button>
        </div>
      </section>

      {showCreateForm ? (
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Create User</h3>
              <p className="settings-panel-copy">Use restrained defaults. Approval is off by default so access is explicit.</p>
            </div>
          </div>

          <form className="mt-4 grid gap-4" onSubmit={createUser}>
            <div className="settings-grid-2">
              <label className="settings-field">
                <span className="settings-label">Email</span>
                <input
                  className="settings-input"
                  placeholder="user@example.com"
                  type="email"
                  value={newUserEmail}
                  onChange={(event) => setNewUserEmail(event.target.value)}
                  required
                />
              </label>

              <label className="settings-field">
                <span className="settings-label">Temporary password</span>
                <input
                  className="settings-input"
                  placeholder="Enter a temporary password"
                  type="password"
                  value={newUserPassword}
                  onChange={(event) => setNewUserPassword(event.target.value)}
                  required
                />
              </label>
            </div>

            <div className="settings-note-list">
              <p>{passwordPolicySummary(securitySettings)}</p>
            </div>

            <div className="settings-toolbar">
              <label className="inline-flex items-center gap-2 text-sm">
                <input checked={newUserApproved} onChange={(event) => setNewUserApproved(event.target.checked)} type="checkbox" />
                Approve immediately
              </label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input checked={newUserSysadmin} onChange={(event) => setNewUserSysadmin(event.target.checked)} type="checkbox" />
                Grant sysadmin
              </label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input checked={newUserAllProjects} onChange={(event) => setNewUserAllProjects(event.target.checked)} type="checkbox" />
                Add to every project
              </label>
            </div>

            {newUserAllProjects ? (
              <label className="settings-field">
                <span className="settings-label">Role for all projects</span>
                <select
                  className="settings-select max-w-[220px]"
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

            <div className="settings-toolbar">
              <button className="settings-button-primary" type="submit">
                Create User
              </button>
              <button
                className="settings-button"
                onClick={() => setShowCreateForm(false)}
                type="button"
              >
                Cancel
              </button>
            </div>
          </form>
        </section>
      ) : null}

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Directory</h3>
            <p className="settings-panel-copy">Dense user inventory with predictable filters and one clear detail path.</p>
          </div>
        </div>

        <form className="mt-4 grid gap-4" onSubmit={applyFilters}>
          <div className="settings-grid-3">
            <label className="settings-field">
              <span className="settings-label">Search</span>
              <input
                className="settings-input"
                placeholder="Email or project name"
                value={filterDraft.search}
                onChange={(event) => setFilterDraft((prev) => ({ ...prev, search: event.target.value }))}
              />
            </label>

            <label className="settings-field">
              <span className="settings-label">Activity</span>
              <select
                className="settings-select"
                value={filterDraft.is_active}
                onChange={(event) => setFilterDraft((prev) => ({ ...prev, is_active: event.target.value }))}
              >
                <option value="all">All</option>
                <option value="true">Active</option>
                <option value="false">Disabled</option>
              </select>
            </label>

            <label className="settings-field">
              <span className="settings-label">Approval</span>
              <select
                className="settings-select"
                value={filterDraft.is_approved}
                onChange={(event) => setFilterDraft((prev) => ({ ...prev, is_approved: event.target.value }))}
              >
                <option value="all">All</option>
                <option value="true">Approved</option>
                <option value="false">Pending approval</option>
              </select>
            </label>

            <label className="settings-field">
              <span className="settings-label">System role</span>
              <select
                className="settings-select"
                value={filterDraft.is_sysadmin}
                onChange={(event) => setFilterDraft((prev) => ({ ...prev, is_sysadmin: event.target.value }))}
              >
                <option value="all">All</option>
                <option value="true">Sysadmins</option>
                <option value="false">Standard users</option>
              </select>
            </label>

            <label className="settings-field">
              <span className="settings-label">Project</span>
              <select
                className="settings-select"
                value={filterDraft.project_id}
                onChange={(event) => setFilterDraft((prev) => ({ ...prev, project_id: event.target.value }))}
              >
                <option value="all">All projects</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="settings-toolbar">
            <button className="settings-button-primary" type="submit">
              Apply Filters
            </button>
            <button className="settings-button" onClick={clearFilters} type="button">
              Clear
            </button>
          </div>
        </form>

        {membershipPreviewError ? (
          <div className="mt-4">
            <StatusBanner tone="warning" title="Project access preview is unavailable">
              <p>
                {membershipPreviewError} User rows are current, but project access counts were not applied. Retry the directory or open a user to load that identity's access details.
              </p>
            </StatusBanner>
          </div>
        ) : membershipPreviewLimited ? (
          <div className="mt-4">
            <StatusBanner tone="warning" title="Project access preview is partial">
              <p>
                The directory rows are usable, but project membership previews reached the client safety limit. Open a user to review that identity's bounded access list, or narrow the directory filters.
              </p>
            </StatusBanner>
          </div>
        ) : null}

        {loading ? (
          <div className="mt-4">
            <StatePanel title="Loading Users" description="Fetching the current directory view." />
          </div>
        ) : directoryError ? (
          <div className="mt-4">
            <StatePanel
              actions={
                <button className="settings-button" onClick={() => refreshDirectory(filters, cursor).catch(() => undefined)} type="button">
                  Retry directory
                </button>
              }
              title="User Directory Unavailable"
              description={`${directoryError} No directory rows were applied. Retry this request or adjust the filters.`}
              tone="error"
            />
          </div>
        ) : users.length === 0 ? (
          <div className="mt-4 settings-empty">No users matched the current filters.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <caption className="sr-only">Users matching the current directory filters</caption>
              <thead>
                <tr>
                  <th>User</th>
                  <th>State</th>
                  <th>Project access</th>
                  <th>Created</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const assigned = membershipsByUserId.get(user.id) || [];
                  const preview = assigned.slice(0, 2).map((membership) => membership.project_name).join(", ");
                  return (
                    <tr key={user.id}>
                      <td>
                        <div className="font-semibold">{user.email}</div>
                        <div className="settings-meta">{user.id}</div>
                      </td>
                      <td>
                        <div className="settings-badge-row">
                          {userStateBadges(user).map((badge) => (
                            <span className={badge.className} key={badge.label}>
                              {badge.label}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td>
                        {membershipPreviewError ? (
                          <>
                            <div>Preview unavailable</div>
                            <div className="settings-meta">Open user for access details.</div>
                          </>
                        ) : (
                          <>
                            <div>{assigned.length} project{assigned.length === 1 ? "" : "s"}</div>
                            {preview ? <div className="settings-meta">{preview}{assigned.length > 2 ? ", ..." : ""}</div> : null}
                          </>
                        )}
                      </td>
                      <td>{formatDate(user.created_at)}</td>
                      <td className="text-right">
                        <Link className="settings-button" to={`/settings/users/${user.id}`}>
                          Manage
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4 settings-toolbar">
          <button className="settings-button" disabled={loading || history.length === 0} onClick={previousPage} type="button">
            Previous
          </button>
          <button className="settings-button" disabled={loading || !nextCursor} onClick={nextPage} type="button">
            Next
          </button>
        </div>
      </section>
    </div>
  );
}
