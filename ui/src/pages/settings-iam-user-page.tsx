import { useEffect, useMemo, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";

import { Dialog } from "@/components/dialog";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { Membership, PROJECT_ROLES, Project, rolePillClass, UserRow } from "@/lib/iam";
import type { SettingsOutletContext } from "@/pages/settings-layout";

function badgeClass(state: "positive" | "warning" | "neutral"): string {
  if (state === "positive") return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200";
  if (state === "warning") return "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200";
  return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";
}

function assignmentKey(membership: Membership): string {
  return `${membership.project_id}:${membership.user_id}`;
}

type SecuritySettings = {
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
};

type PendingIdentityAction = "toggle_active" | "toggle_approval" | "toggle_sysadmin" | "assign_baseline";

export function SettingsIamUserPage() {
  const { me } = useOutletContext<SettingsOutletContext>();
  const { userId } = useParams<{ userId: string }>();

  const [user, setUser] = useState<UserRow | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [loading, setLoading] = useState(true);
  const [securitySettings, setSecuritySettings] = useState<SecuritySettings | null>(null);

  const [membershipRoleDraft, setMembershipRoleDraft] = useState<Record<string, string>>({});
  const [newProjectId, setNewProjectId] = useState("");
  const [newProjectRole, setNewProjectRole] = useState("viewer");
  const [baselineRole, setBaselineRole] = useState("viewer");
  const [baselineOverwrite, setBaselineOverwrite] = useState(false);
  const [passwordDraft, setPasswordDraft] = useState("");
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [membershipToRemove, setMembershipToRemove] = useState<Membership | null>(null);
  const [pendingIdentityAction, setPendingIdentityAction] = useState<PendingIdentityAction | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const loadUser = async () => {
    if (!userId) {
      setUser(null);
      return;
    }
    const data = await apiFetch(`/users/${userId}`);
    setUser((data || null) as UserRow | null);
  };

  const loadProjects = async () => {
    const data = await apiFetch("/settings/projects");
    setProjects((data || []) as Project[]);
  };

  const loadSecuritySettings = async () => {
    const data = await apiFetch("/auth/security-settings");
    setSecuritySettings(data as SecuritySettings);
  };

  const loadMemberships = async () => {
    if (!userId) {
      setMemberships([]);
      return;
    }
    const rows = await apiFetchAllPages<Membership>((cursor) => {
      const query = new URLSearchParams({ limit: "200" });
      if (cursor) query.set("cursor", cursor);
      query.append("user_ids", userId);
      return `/settings/rbac/project-memberships?${query.toString()}`;
    });
    setMemberships(rows);
  };

  const refreshPage = async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([loadUser(), loadProjects(), loadMemberships(), loadSecuritySettings()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load identity details");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refreshPage().catch(() => undefined);
  }, [userId]);

  const assigned = useMemo(() => {
    const filtered = [...memberships];
    filtered.sort((a, b) => a.project_name.localeCompare(b.project_name));
    return filtered;
  }, [memberships]);

  const availableProjects = useMemo(() => {
    const assignedProjectIds = new Set(assigned.map((membership) => membership.project_id));
    return projects.filter((project) => !assignedProjectIds.has(project.id));
  }, [assigned, projects]);

  const selectedProjectId = useMemo(() => {
    if (newProjectId && availableProjects.some((project) => project.id === newProjectId)) return newProjectId;
    return availableProjects[0]?.id || "";
  }, [availableProjects, newProjectId]);

  useEffect(() => {
    if (selectedProjectId !== newProjectId) {
      setNewProjectId(selectedProjectId);
    }
  }, [newProjectId, selectedProjectId]);

  async function patchUser(payload: Record<string, unknown>, successMessage: string) {
    if (!userId) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/users/${userId}`, { method: "PATCH", body: JSON.stringify(payload) });
      setInfo(successMessage);
      await refreshPage();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update identity");
    }
  }

  async function resetPassword() {
    if (!user) return;
    if (!passwordDraft) {
      setError("A temporary password is required.");
      return;
    }
    await patchUser({ password: passwordDraft }, "Password reset complete.");
    setPasswordDraft("");
    setPasswordDialogOpen(false);
  }

  async function upsertMembership(projectId: string, role: string, successMessage: string) {
    if (!userId) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/settings/rbac/project-memberships", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, user_id: userId, role }),
      });
      setInfo(successMessage);
      await refreshPage();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update project assignment");
    }
  }

  async function removeMembership(membership: Membership) {
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/rbac/project-memberships/${membership.project_id}/${membership.user_id}`, { method: "DELETE" });
      setInfo("Project access removed.");
      await refreshPage();
      setMembershipToRemove(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove project assignment");
    }
  }

  async function assignAllProjects() {
    if (!userId || !user) return;
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/settings/rbac/users/${userId}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role: baselineRole, overwrite_existing: baselineOverwrite }),
      });
      const updated = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      const skipped = Array.isArray(data?.skipped_projects) ? data.skipped_projects.length : 0;
      setInfo(
        skipped > 0
          ? `Applied baseline for ${user.email}: ${updated} membership(s) updated, ${skipped} project(s) skipped to preserve admin access.`
          : `Applied baseline for ${user.email}: ${updated} membership(s) updated.`,
      );
      await refreshPage();
      setPendingIdentityAction(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply baseline");
    }
  }

  async function confirmIdentityAction() {
    if (!user || !pendingIdentityAction) return;
    if (pendingIdentityAction === "toggle_active") {
      await patchUser({ is_active: !user.is_active }, user.is_active ? `Disabled ${user.email}.` : `Enabled ${user.email}.`);
      setPendingIdentityAction(null);
      return;
    }
    if (pendingIdentityAction === "toggle_approval") {
      await patchUser({ is_approved: !user.is_approved }, user.is_approved ? `Revoked approval for ${user.email}.` : `Approved ${user.email}.`);
      setPendingIdentityAction(null);
      return;
    }
    if (pendingIdentityAction === "toggle_sysadmin") {
      await patchUser({ is_sysadmin: !user.is_sysadmin }, user.is_sysadmin ? `Removed system admin from ${user.email}.` : `Granted system admin to ${user.email}.`);
      setPendingIdentityAction(null);
      return;
    }
    await assignAllProjects();
  }

  return (
    <>
      {error ? (
        <div className="workspace-section">
          <StatusBanner tone="error" title="Identity Update Failed">
            <p>{error}</p>
          </StatusBanner>
        </div>
      ) : null}
      {info ? (
        <div className="workspace-section">
          <StatusBanner tone="success" title="Identity Update">
            <p>{info}</p>
          </StatusBanner>
        </div>
      ) : null}

      <div className="workspace-section">
        <div className="workspace-card">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <Link className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500 hover:text-slate-700 dark:hover:text-slate-200" to="/settings/iam">
                Back to IAM directory
              </Link>
              <h2 className="mt-2 text-2xl font-semibold">{user?.email || "Identity"}</h2>
              <p className="mt-1 text-sm text-slate-500">Manage one user at a time: lifecycle controls, project roles, and baseline assignments.</p>
            </div>
            <button
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
              onClick={() => refreshPage().catch(() => undefined)}
              type="button"
            >
              Refresh
            </button>
          </div>

          {loading ? (
            <div className="mt-4">
              <StatePanel title="Loading Identity" description="Fetching lifecycle state, project assignments, and password policy." />
            </div>
          ) : null}

          {!loading && !user ? (
            <div className="mt-4">
              <StatePanel title="Identity Not Found" description="The requested user could not be found in the IAM directory." tone="warning" />
            </div>
          ) : null}

          {user ? (
            <div className="mt-5 space-y-4">
              <div className="flex flex-wrap gap-2 text-xs font-semibold uppercase tracking-wide">
                <span className={`rounded-full px-2.5 py-1 ${user.is_sysadmin ? badgeClass("positive") : badgeClass("neutral")}`}>
                  {user.is_sysadmin ? "System admin" : "Standard user"}
                </span>
                <span className={`rounded-full px-2.5 py-1 ${user.is_active ? badgeClass("positive") : badgeClass("warning")}`}>
                  {user.is_active ? "Active" : "Disabled"}
                </span>
                <span className={`rounded-full px-2.5 py-1 ${user.is_approved ? badgeClass("positive") : badgeClass("warning")}`}>
                  {user.is_approved ? "Approved" : "Pending approval"}
                </span>
                <span className={`rounded-full px-2.5 py-1 ${badgeClass("neutral")}`}>{assigned.length} project{assigned.length === 1 ? "" : "s"}</span>
              </div>

              <div className="rounded-2xl bg-slate-50 p-4 dark:bg-slate-900/60">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Current access tags</p>
                {assigned.length === 0 ? (
                  <p className="mt-2 text-sm text-slate-500">No project assignments yet.</p>
                ) : (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {assigned.map((membership) => (
                      <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-700 dark:border-slate-700 dark:text-slate-200" key={assignmentKey(membership)}>
                        <span>{membership.project_name}</span>
                        <span className={`rounded-full px-2 py-0.5 font-semibold uppercase tracking-wide ${rolePillClass(membership.role)}`}>
                          {membership.role}
                        </span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {user ? (
        <div className="workspace-section grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_360px]">
          <section className="workspace-card space-y-4">
            <div>
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Project Access</p>
              <h3 className="mt-2 text-xl font-semibold">Current assignments</h3>
              <p className="mt-1 text-sm text-slate-500">Each row shows the current role and the only actions that change it.</p>
            </div>

            {assigned.length === 0 ? (
              <StatePanel title="No Project Assignments" description="Grant access to one or more projects before this identity can work with customer data." />
            ) : (
              <div className="space-y-3">
                {assigned.map((membership) => {
                  const key = assignmentKey(membership);
                  const draftRole = membershipRoleDraft[key] || membership.role;
                  const changed = draftRole !== membership.role;
                  return (
                    <article className="rounded-2xl border border-slate-200 p-4 dark:border-slate-800" key={key}>
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h4 className="font-semibold">{membership.project_name}</h4>
                          <p className="mt-1 text-xs text-slate-500">Current role: {membership.role}</p>
                        </div>
                        <span className={`rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ${rolePillClass(membership.role)}`}>
                          {membership.role}
                        </span>
                      </div>

                      <div className="mt-4 grid gap-3 md:grid-cols-[180px_auto_auto] md:items-end">
                        <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                          New role
                          <select
                            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                            value={draftRole}
                            onChange={(event) => setMembershipRoleDraft((prev) => ({ ...prev, [key]: event.target.value }))}
                          >
                            {PROJECT_ROLES.map((role) => (
                              <option key={role} value={role}>
                                {role}
                              </option>
                            ))}
                          </select>
                        </label>
                        <button
                          className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700 disabled:opacity-50"
                          disabled={!changed}
                          onClick={() =>
                            upsertMembership(
                              membership.project_id,
                              draftRole,
                              `Updated ${membership.project_name} access for ${membership.user_email}.`,
                            )
                          }
                          type="button"
                        >
                          Save role
                        </button>
                        <button
                          className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700"
                          onClick={() => setMembershipToRemove(membership)}
                          type="button"
                        >
                          Remove access
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <div className="space-y-4">
            <section className="workspace-card space-y-4">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Grant Access</p>
                <h3 className="mt-2 text-xl font-semibold">Add one project</h3>
                <p className="mt-1 text-sm text-slate-500">Pick a project the user does not already have.</p>
              </div>

              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                Project
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={selectedProjectId}
                  onChange={(event) => setNewProjectId(event.target.value)}
                  disabled={availableProjects.length === 0}
                >
                  {availableProjects.length === 0 ? <option value="">No unassigned projects</option> : null}
                  {availableProjects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                Role
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={newProjectRole}
                  onChange={(event) => setNewProjectRole(event.target.value)}
                >
                  {PROJECT_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </label>

              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700 disabled:opacity-50"
                disabled={!selectedProjectId}
                onClick={() => upsertMembership(selectedProjectId, newProjectRole, `Added ${user.email} to the selected project.`)}
                type="button"
              >
                Grant project access
              </button>
            </section>

            <section className="workspace-card space-y-4">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Baseline Access</p>
                <h3 className="mt-2 text-xl font-semibold">Apply to all projects</h3>
                <p className="mt-1 text-sm text-slate-500">Use this when the same role should exist across the full project catalog.</p>
              </div>

              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                Baseline role
                <select
                  className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  value={baselineRole}
                  onChange={(event) => setBaselineRole(event.target.value)}
                >
                  {PROJECT_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </label>

              <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                <input checked={baselineOverwrite} onChange={(event) => setBaselineOverwrite(event.target.checked)} type="checkbox" />
                Replace existing project roles with this baseline
              </label>

              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700"
                onClick={() => setPendingIdentityAction("assign_baseline")}
                type="button"
              >
                Apply baseline
              </button>
            </section>

            <section className="workspace-card space-y-3">
              <div>
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Identity Controls</p>
                <h3 className="mt-2 text-xl font-semibold">Lifecycle</h3>
                <p className="mt-1 text-sm text-slate-500">These actions affect all project access for the user.</p>
              </div>

              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700"
                onClick={() => {
                  setPasswordDraft("");
                  setPasswordDialogOpen(true);
                }}
                type="button"
              >
                Reset password
              </button>
              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700 disabled:opacity-50"
                disabled={user.id === me.id}
                onClick={() => setPendingIdentityAction("toggle_active")}
                type="button"
              >
                {user.is_active ? "Disable identity" : "Enable identity"}
              </button>
              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700 disabled:opacity-50"
                disabled={user.id === me.id}
                onClick={() => setPendingIdentityAction("toggle_approval")}
                type="button"
              >
                {user.is_approved ? "Revoke approval" : "Approve identity"}
              </button>
              <button
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-wide dark:border-slate-700 disabled:opacity-50"
                disabled={user.id === me.id}
                onClick={() => setPendingIdentityAction("toggle_sysadmin")}
                type="button"
              >
                {user.is_sysadmin ? "Remove system admin" : "Grant system admin"}
              </button>
              {user.id === me.id ? <p className="text-xs text-slate-500">Self-lockout protections block disabling your own identity or admin status here.</p> : null}
            </section>
          </div>
        </div>
      ) : null}

      <Dialog
        open={passwordDialogOpen}
        title="Reset password"
        description={user ? `Set a temporary password for ${user.email}.` : undefined}
        onClose={() => {
          setPasswordDialogOpen(false);
          setPasswordDraft("");
        }}
        footer={
          <>
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              onClick={() => {
                setPasswordDialogOpen(false);
                setPasswordDraft("");
              }}
              type="button"
            >
              Cancel
            </button>
            <button
              className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white dark:bg-slate-100 dark:text-slate-900"
              onClick={resetPassword}
              type="button"
            >
              Apply temporary password
            </button>
          </>
        }
      >
        <div className="space-y-4">
          {securitySettings ? (
            <StatusBanner tone="info" title="Current password policy">
              <div className="flex flex-wrap gap-2 text-xs">
                <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40">Min length {securitySettings.password_min_length}</span>
                {securitySettings.password_require_lowercase ? <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40">Lowercase</span> : null}
                {securitySettings.password_require_uppercase ? <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40">Uppercase</span> : null}
                {securitySettings.password_require_number ? <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40">Number</span> : null}
                {securitySettings.password_require_special ? <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40">Special character</span> : null}
              </div>
            </StatusBanner>
          ) : null}
          <label className="block text-sm">
            Temporary password
            <input
              className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
              type="password"
              value={passwordDraft}
              onChange={(event) => setPasswordDraft(event.target.value)}
            />
          </label>
        </div>
      </Dialog>

      <Dialog
        open={!!membershipToRemove}
        title="Remove project access"
        description={
          membershipToRemove
            ? `This removes ${membershipToRemove.user_email} from ${membershipToRemove.project_name}.`
            : undefined
        }
        onClose={() => setMembershipToRemove(null)}
        footer={
          <>
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              onClick={() => setMembershipToRemove(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white dark:bg-slate-100 dark:text-slate-900"
              onClick={() => {
                if (membershipToRemove) {
                  removeMembership(membershipToRemove).catch(() => undefined);
                }
              }}
              type="button"
            >
              Remove access
            </button>
          </>
        }
      >
        <p className="text-sm text-slate-600 dark:text-slate-300">The user will immediately lose access to this project's runs and inventory.</p>
      </Dialog>

      <Dialog
        open={!!pendingIdentityAction}
        title={
          pendingIdentityAction === "toggle_active"
            ? user?.is_active
              ? "Disable identity"
              : "Enable identity"
            : pendingIdentityAction === "toggle_approval"
              ? user?.is_approved
                ? "Revoke approval"
                : "Approve identity"
              : pendingIdentityAction === "toggle_sysadmin"
                ? user?.is_sysadmin
                  ? "Remove system admin"
                  : "Grant system admin"
                : "Apply baseline access"
        }
        description={
          pendingIdentityAction === "assign_baseline"
            ? `Apply the ${baselineRole} baseline across every project for this user.`
            : user
              ? `This change affects ${user.email} across the full deployment.`
              : undefined
        }
        onClose={() => setPendingIdentityAction(null)}
        footer={
          <>
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              onClick={() => setPendingIdentityAction(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white dark:bg-slate-100 dark:text-slate-900"
              onClick={() => confirmIdentityAction().catch(() => undefined)}
              type="button"
            >
              Confirm
            </button>
          </>
        }
      >
        {pendingIdentityAction === "assign_baseline" ? (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Existing roles {baselineOverwrite ? "will" : "will not"} be overwritten.
          </p>
        ) : (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Review the current identity state before applying this global change.
          </p>
        )}
      </Dialog>
    </>
  );
}
