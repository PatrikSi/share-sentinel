import { FormEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";

import { Dialog } from "@/components/dialog";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { Membership, PROJECT_ROLES, Project, UserRow } from "@/lib/iam";
import type { SettingsOutletContext } from "@/pages/settings-layout";

type SecuritySettings = {
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
};

type IdentityAction = "toggle_active" | "toggle_approval" | "toggle_sysadmin" | "assign_baseline";

function membershipKey(membership: Membership): string {
  return `${membership.project_id}:${membership.user_id}`;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
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

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function SettingsIamUserPage() {
  const { me } = useOutletContext<SettingsOutletContext>();
  const { userId } = useParams<{ userId: string }>();
  const userIdRef = useRef(userId);
  userIdRef.current = userId;

  const [user, setUser] = useState<UserRow | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [securitySettings, setSecuritySettings] = useState<SecuritySettings | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [membershipsLimited, setMembershipsLimited] = useState(false);

  const [membershipRoleDraft, setMembershipRoleDraft] = useState<Record<string, string>>({});
  const [newProjectId, setNewProjectId] = useState("");
  const [newProjectRole, setNewProjectRole] = useState("viewer");
  const [baselineRole, setBaselineRole] = useState("viewer");
  const [baselineOverwrite, setBaselineOverwrite] = useState(false);
  const [passwordDraft, setPasswordDraft] = useState("");
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [membershipToRemove, setMembershipToRemove] = useState<Membership | null>(null);
  const [pendingIdentityAction, setPendingIdentityAction] = useState<IdentityAction | null>(null);

  function isCurrentUser(targetUserId: string, signal?: AbortSignal): boolean {
    return !signal?.aborted && userIdRef.current === targetUserId;
  }

  async function refreshPage(targetUserId = userId, signal?: AbortSignal) {
    if (!targetUserId || !isCurrentUser(targetUserId, signal)) return;
    setLoading(true);
    setError(null);
    try {
      const [userData, projectData, membershipResult, settingsData] = await Promise.all([
        apiFetch(`/users/${targetUserId}`, { signal }),
        apiFetch("/settings/projects", { signal }),
        apiFetchAllPages<Membership>((cursor) => {
          const query = new URLSearchParams({ limit: "250" });
          if (cursor) query.set("cursor", cursor);
          query.append("user_ids", targetUserId);
          return `/settings/rbac/project-memberships?${query.toString()}`;
        }, { signal }, { maxPages: 20, maxItems: 5_000, maxDurationMs: 15_000 }),
        apiFetch("/auth/security-settings", { signal }),
      ]);
      if (!isCurrentUser(targetUserId, signal)) return;
      setUser((userData || null) as UserRow | null);
      setProjects((projectData || []) as Project[]);
      setMemberships(membershipResult.items);
      setMembershipsLimited(membershipResult.truncated);
      setSecuritySettings(settingsData as SecuritySettings);
    } catch (err) {
      if (!isCurrentUser(targetUserId, signal) || isAbortError(err)) return;
      setError(err instanceof Error ? err.message : "Failed to load user details");
    } finally {
      if (isCurrentUser(targetUserId, signal)) setLoading(false);
    }
  }

  useLayoutEffect(() => {
    setUser(null);
    setProjects([]);
    setMemberships([]);
    setSecuritySettings(null);
    setMembershipsLimited(false);
    setMembershipRoleDraft({});
    setNewProjectId("");
    setNewProjectRole("viewer");
    setBaselineRole("viewer");
    setBaselineOverwrite(false);
    setPasswordDraft("");
    setPasswordDialogOpen(false);
    setMembershipToRemove(null);
    setPendingIdentityAction(null);
    setError(userId ? null : "No user identifier was provided.");
    setInfo(null);
    setLoading(!!userId);
  }, [userId]);

  useEffect(() => {
    if (!userId) return;
    const targetUserId = userId;
    const controller = new AbortController();
    refreshPage(targetUserId, controller.signal).catch(() => undefined);
    return () => controller.abort();
  }, [userId]);

  const assigned = useMemo(() => {
    const copy = [...memberships];
    copy.sort((a, b) => a.project_name.localeCompare(b.project_name));
    return copy;
  }, [memberships]);

  const availableProjects = useMemo(() => {
    const assignedProjectIds = new Set(assigned.map((membership) => membership.project_id));
    return projects.filter((project) => !assignedProjectIds.has(project.id));
  }, [assigned, projects]);

  useEffect(() => {
    if (!newProjectId && availableProjects.length > 0) {
      setNewProjectId(availableProjects[0].id);
    }
    if (newProjectId && !availableProjects.some((project) => project.id === newProjectId)) {
      setNewProjectId(availableProjects[0]?.id || "");
    }
  }, [availableProjects, newProjectId]);

  async function patchUser(payload: Record<string, unknown>, successMessage: string): Promise<boolean> {
    const targetUserId = user?.id;
    if (!targetUserId || targetUserId !== userIdRef.current) return false;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/users/${targetUserId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      if (!isCurrentUser(targetUserId)) return false;
      setInfo(successMessage);
      await refreshPage(targetUserId);
      return isCurrentUser(targetUserId);
    } catch (err) {
      if (!isCurrentUser(targetUserId)) return false;
      setError(err instanceof Error ? err.message : "Failed to update user");
      return false;
    }
  }

  async function resetPassword() {
    if (!user) return;
    if (!passwordDraft) {
      setError("A temporary password is required.");
      return;
    }
    const updated = await patchUser({ password: passwordDraft }, `Password reset for ${user.email}.`);
    if (updated) {
      setPasswordDraft("");
      setPasswordDialogOpen(false);
    }
  }

  async function upsertMembership(projectId: string, role: string, successMessage: string) {
    const targetUserId = user?.id;
    if (!targetUserId || targetUserId !== userIdRef.current) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch("/settings/rbac/project-memberships", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, user_id: targetUserId, role }),
      });
      if (!isCurrentUser(targetUserId)) return;
      setInfo(successMessage);
      await refreshPage(targetUserId);
    } catch (err) {
      if (!isCurrentUser(targetUserId)) return;
      setError(err instanceof Error ? err.message : "Failed to update project access");
    }
  }

  async function removeMembership(membership: Membership) {
    const targetUserId = user?.id;
    if (!targetUserId || targetUserId !== userIdRef.current || membership.user_id !== targetUserId) return;
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/settings/rbac/project-memberships/${membership.project_id}/${membership.user_id}`, { method: "DELETE" });
      if (!isCurrentUser(targetUserId)) return;
      setInfo("Project access removed.");
      setMembershipToRemove(null);
      await refreshPage(targetUserId);
    } catch (err) {
      if (!isCurrentUser(targetUserId)) return;
      setError(err instanceof Error ? err.message : "Failed to remove project access");
    }
  }

  async function assignAllProjects() {
    const targetUserId = user?.id;
    if (!targetUserId || targetUserId !== userIdRef.current) return;
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch(`/settings/rbac/users/${targetUserId}/assign-all-projects`, {
        method: "POST",
        body: JSON.stringify({ role: baselineRole, overwrite_existing: baselineOverwrite }),
      });
      if (!isCurrentUser(targetUserId)) return;
      const updated = typeof data?.assigned_projects === "number" ? data.assigned_projects : 0;
      const skipped = Array.isArray(data?.skipped_projects) ? data.skipped_projects.length : 0;
      setInfo(
        skipped > 0
          ? `Baseline applied: ${updated} memberships updated, ${skipped} projects skipped to preserve admin coverage.`
          : `Baseline applied: ${updated} memberships updated.`,
      );
      setPendingIdentityAction(null);
      await refreshPage(targetUserId);
    } catch (err) {
      if (!isCurrentUser(targetUserId)) return;
      setError(err instanceof Error ? err.message : "Failed to apply project baseline");
    }
  }

  async function confirmIdentityAction() {
    if (!user || !pendingIdentityAction) return;

    if (pendingIdentityAction === "toggle_active") {
      const updated = await patchUser({ is_active: !user.is_active }, user.is_active ? `Disabled ${user.email}.` : `Enabled ${user.email}.`);
      if (updated) setPendingIdentityAction(null);
      return;
    }
    if (pendingIdentityAction === "toggle_approval") {
      const updated = await patchUser(
        { is_approved: !user.is_approved },
        user.is_approved ? `Removed approval for ${user.email}.` : `Approved ${user.email}.`,
      );
      if (updated) setPendingIdentityAction(null);
      return;
    }
    if (pendingIdentityAction === "toggle_sysadmin") {
      const updated = await patchUser(
        { is_sysadmin: !user.is_sysadmin },
        user.is_sysadmin ? `Removed sysadmin from ${user.email}.` : `Granted sysadmin to ${user.email}.`,
      );
      if (updated) setPendingIdentityAction(null);
      return;
    }
    await assignAllProjects();
  }

  function identityActionCopy(action: IdentityAction): { title: string; description: string; confirmLabel: string } {
    if (!user) {
      return { title: "Confirm change", description: "", confirmLabel: "Confirm" };
    }
    if (action === "toggle_active") {
      return user.is_active
        ? {
            title: `Disable ${user.email}?`,
            description: "This blocks login and revokes active refresh sessions.",
            confirmLabel: "Disable user",
          }
        : {
            title: `Enable ${user.email}?`,
            description: "This restores sign-in capability for the account.",
            confirmLabel: "Enable user",
          };
    }
    if (action === "toggle_approval") {
      return user.is_approved
        ? {
            title: `Remove approval from ${user.email}?`,
            description: "The account will remain in the directory but will no longer be able to sign in.",
            confirmLabel: "Remove approval",
          }
        : {
            title: `Approve ${user.email}?`,
            description: "The account will be allowed to sign in if it is also active.",
            confirmLabel: "Approve user",
          };
    }
    if (action === "toggle_sysadmin") {
      return user.is_sysadmin
        ? {
            title: `Remove sysadmin from ${user.email}?`,
            description: "This removes access to the global administration workspace.",
            confirmLabel: "Remove sysadmin",
          }
        : {
            title: `Grant sysadmin to ${user.email}?`,
            description: "This grants access to all administration pages and actions.",
            confirmLabel: "Grant sysadmin",
          };
    }
    return {
      title: `Apply ${baselineRole} baseline across all projects?`,
      description: baselineOverwrite
        ? "Existing memberships will be overwritten where allowed."
        : "Only missing project memberships will be created.",
      confirmLabel: "Apply baseline",
    };
  }

  if (loading) {
    return <StatePanel title="Loading User" description="Fetching lifecycle state, password policy, and project access." />;
  }

  if (!user) {
    return error ? (
      <StatePanel
        actions={
          <button className="settings-button" onClick={() => refreshPage().catch(() => undefined)} type="button">
            Retry user details
          </button>
        }
        title="User Details Unavailable"
        description={`${error} No user changes were made.`}
        tone="error"
      />
    ) : (
      <StatePanel title="User Not Found" description="The requested user does not exist or is outside your visible scope." tone="warning" />
    );
  }

  const actionCopy = pendingIdentityAction ? identityActionCopy(pendingIdentityAction) : null;

  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <Link className="settings-meta underline" to="/settings/users">
            Back to Users
          </Link>
          <h2 className="settings-page-title mt-2">{user.email}</h2>
          <p className="settings-page-copy">
            Lifecycle controls, password reset, and project access for one identity.
            {me.id === user.id ? " You are viewing your own account." : ""}
          </p>
        </div>
        <div className="settings-toolbar">
          <button className="settings-button" onClick={() => refreshPage().catch(() => undefined)} type="button">
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
      {membershipsLimited ? (
        <StatusBanner tone="warning" title="Project access list is partial">
          <p>
            The loaded assignments remain usable, but this identity exceeded the client page, item, or time limit. Assignment totals are minimums, and adding from the incomplete “available projects” catalog is disabled.
          </p>
        </StatusBanner>
      ) : null}

      <div className="settings-grid-2">
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Identity State</h3>
              <p className="settings-panel-copy">Primary lifecycle flags and direct admin actions.</p>
            </div>
          </div>

          <div className="mt-4 settings-badge-row">
            <span className={user.is_active ? "settings-badge settings-badge-positive" : "settings-badge settings-badge-warning"}>
              {user.is_active ? "Active" : "Disabled"}
            </span>
            <span className={user.is_approved ? "settings-badge settings-badge-neutral" : "settings-badge settings-badge-warning"}>
              {user.is_approved ? "Approved" : "Pending approval"}
            </span>
            <span className={user.is_sysadmin ? "settings-badge settings-badge-positive" : "settings-badge settings-badge-neutral"}>
              {user.is_sysadmin ? "Sysadmin" : "Standard user"}
            </span>
          </div>

          <dl className="mt-4 grid gap-3 text-sm">
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Created</dt>
              <dd>{formatDate(user.created_at)}</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">User ID</dt>
              <dd className="max-w-[24rem] break-all text-right">{user.id}</dd>
            </div>
          </dl>

          <div className="mt-5 settings-toolbar">
            <button className="settings-button" onClick={() => setPendingIdentityAction("toggle_approval")} type="button">
              {user.is_approved ? "Remove Approval" : "Approve User"}
            </button>
            <button className="settings-button" onClick={() => setPendingIdentityAction("toggle_active")} type="button">
              {user.is_active ? "Disable User" : "Enable User"}
            </button>
            <button className="settings-button" onClick={() => setPendingIdentityAction("toggle_sysadmin")} type="button">
              {user.is_sysadmin ? "Remove Sysadmin" : "Grant Sysadmin"}
            </button>
            <button className="settings-button" onClick={() => setPasswordDialogOpen(true)} type="button">
              Reset Password
            </button>
          </div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Project Baseline</h3>
              <p className="settings-panel-copy">Use one controlled action to add the user across all projects.</p>
            </div>
          </div>

          <div className="mt-4 grid gap-4">
            <label className="settings-field">
              <span className="settings-label">Role</span>
              <select className="settings-select max-w-[220px]" value={baselineRole} onChange={(event) => setBaselineRole(event.target.value)}>
                {PROJECT_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>

            <label className="inline-flex items-center gap-2 text-sm">
              <input checked={baselineOverwrite} onChange={(event) => setBaselineOverwrite(event.target.checked)} type="checkbox" />
              Overwrite existing memberships
            </label>

            <div className="settings-note-list">
              <p>Current project assignments: {membershipsLimited ? `at least ${assigned.length}` : assigned.length}</p>
              <p>Last-project-admin guardrails remain enforced by the API.</p>
            </div>

            <div>
              <button className="settings-button" onClick={() => setPendingIdentityAction("assign_baseline")} type="button">
                Apply Baseline
              </button>
            </div>
          </div>
        </section>
      </div>

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Project Access</h3>
            <p className="settings-panel-copy">Manage explicit memberships one project at a time.</p>
          </div>
        </div>

        <div className="mt-4 settings-grid-2">
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (membershipsLimited || !newProjectId) return;
              upsertMembership(newProjectId, newProjectRole, "Project access updated.").catch(() => undefined);
            }}
          >
            <label className="settings-field">
              <span className="settings-label">Add project</span>
              <select className="settings-select" value={newProjectId} onChange={(event) => setNewProjectId(event.target.value)} disabled={membershipsLimited || availableProjects.length === 0}>
                {membershipsLimited ? <option value="">Unavailable while access is partial</option> : null}
                {!membershipsLimited && availableProjects.length === 0 ? <option value="">No remaining projects</option> : null}
                {availableProjects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="settings-field">
              <span className="settings-label">Role</span>
              <select className="settings-select max-w-[220px]" value={newProjectRole} onChange={(event) => setNewProjectRole(event.target.value)}>
                {PROJECT_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>

            <div>
              <button className="settings-button-primary" type="submit" disabled={membershipsLimited || !newProjectId}>
                Add Project Access
              </button>
            </div>
          </form>

          <div className="settings-panel">
            <h4 className="settings-panel-title">Password policy</h4>
            <p className="settings-panel-copy">{passwordPolicySummary(securitySettings)}</p>
          </div>
        </div>

        {assigned.length === 0 ? (
          <div className="mt-4 settings-empty">
            {membershipsLimited ? "No project assignments were returned before the bounded load stopped." : "This user does not currently belong to any projects."}
          </div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <caption className="sr-only">Project access assigned to this user</caption>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Role</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {assigned.map((membership) => {
                  const key = membershipKey(membership);
                  const draftRole = membershipRoleDraft[key] || membership.role;
                  return (
                    <tr key={key}>
                      <td>
                        <div className="font-semibold">{membership.project_name}</div>
                        <div className="settings-meta">{membership.project_id}</div>
                      </td>
                      <td>
                        <select
                          className="settings-select max-w-[220px]"
                          value={draftRole}
                          onChange={(event) => setMembershipRoleDraft((prev) => ({ ...prev, [key]: event.target.value }))}
                        >
                          {PROJECT_ROLES.map((role) => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="text-right">
                        <div className="settings-toolbar justify-end">
                          <button
                            className="settings-button"
                            onClick={() => upsertMembership(membership.project_id, draftRole, "Project access updated.").catch(() => undefined)}
                            type="button"
                          >
                            Save Role
                          </button>
                          <button className="settings-button-danger" onClick={() => setMembershipToRemove(membership)} type="button">
                            Remove
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="settings-panel">
        <div className="settings-danger">
          <h3 className="settings-panel-title">Guardrails</h3>
          <ul className="settings-note-list">
            <li>The API blocks self-lockout and protects the last active approved sysadmin.</li>
            <li>The API also blocks removing the last admin from any project.</li>
            <li>Use the project baseline action carefully when broad access changes are intended.</li>
          </ul>
        </div>
      </section>

      <Dialog
        open={passwordDialogOpen}
        onClose={() => setPasswordDialogOpen(false)}
        title={`Reset password for ${user.email}`}
        description="Set a temporary password that meets the active password policy."
        footer={
          <>
            <button className="settings-button" onClick={() => setPasswordDialogOpen(false)} type="button">
              Cancel
            </button>
            <button className="settings-button-primary" onClick={() => resetPassword().catch(() => undefined)} type="button">
              Reset Password
            </button>
          </>
        }
      >
        <label className="settings-field">
          <span className="settings-label">Temporary password</span>
          <input
            className="settings-input"
            type="password"
            value={passwordDraft}
            onChange={(event) => setPasswordDraft(event.target.value)}
          />
        </label>
      </Dialog>

      <Dialog
        open={membershipToRemove !== null}
        onClose={() => setMembershipToRemove(null)}
        title={membershipToRemove ? `Remove access to ${membershipToRemove.project_name}?` : "Remove project access"}
        description="This removes the user from the selected project. The API will block the change if it would remove the last project admin."
        footer={
          <>
            <button className="settings-button" onClick={() => setMembershipToRemove(null)} type="button">
              Cancel
            </button>
            <button
              className="settings-button-danger"
              onClick={() => {
                if (membershipToRemove) {
                  removeMembership(membershipToRemove).catch(() => undefined);
                }
              }}
              type="button"
            >
              Remove Access
            </button>
          </>
        }
      >
        {membershipToRemove ? (
          <div className="settings-note-list">
            <p>User: {membershipToRemove.user_email}</p>
            <p>Role to remove: {membershipToRemove.role}</p>
            <p>Project: {membershipToRemove.project_name}</p>
          </div>
        ) : null}
      </Dialog>

      <Dialog
        open={pendingIdentityAction !== null}
        onClose={() => setPendingIdentityAction(null)}
        title={actionCopy?.title || "Confirm change"}
        description={actionCopy?.description || ""}
        footer={
          <>
            <button className="settings-button" onClick={() => setPendingIdentityAction(null)} type="button">
              Cancel
            </button>
            <button className="settings-button-primary" onClick={() => confirmIdentityAction().catch(() => undefined)} type="button">
              {actionCopy?.confirmLabel || "Confirm"}
            </button>
          </>
        }
      >
        <div className="settings-note-list">
          <p>User: {user.email}</p>
          {pendingIdentityAction === "assign_baseline" ? (
            <p>
              Baseline role: {baselineRole}
              {baselineOverwrite ? " with overwrite enabled." : " without overwriting existing memberships."}
            </p>
          ) : (
            <p>Review the account state before applying this change.</p>
          )}
        </div>
      </Dialog>
    </div>
  );
}
