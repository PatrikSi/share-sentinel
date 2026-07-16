import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Dialog } from "@/components/dialog";
import { StatePanel } from "@/components/state-panel";
import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { Membership } from "@/lib/iam";

type ProjectDetail = {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
  admin_count: number;
  token_count: number;
  active_token_count: number;
  run_count: number;
  artifact_count: number;
  blocking_run_count: number;
  has_blocking_runs: boolean;
  last_run_at: string | null;
  run_status_counts: Record<string, number>;
  blocking_runs: Array<{
    id: string;
    name: string;
    status: string;
    created_at: string;
  }>;
};

type TokenRow = {
  id: string;
  user_email: string;
  name: string;
  role: string;
  scopes: string[];
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
};

type ActivityRow = {
  id: number;
  ts: string;
  actor_email: string | null;
  actor_user_id: string | null;
  action: string;
  object_type: string;
  object_id: string;
  metadata: Record<string, unknown>;
};

function formatDateTime(value: string | null): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function tokenStatus(token: TokenRow): { label: string; className: string } {
  if (token.revoked_at) return { label: "Revoked", className: "settings-badge settings-badge-danger" };
  if (!token.expires_at) return { label: "No expiry", className: "settings-badge settings-badge-warning" };
  const expiresAt = new Date(token.expires_at);
  if (!Number.isNaN(expiresAt.getTime()) && expiresAt.getTime() < Date.now()) {
    return { label: "Expired", className: "settings-badge settings-badge-warning" };
  }
  return { label: "Active", className: "settings-badge settings-badge-positive" };
}

function summarizeScopes(scopes: string[]): string {
  if (scopes.length === 0) return "Default role scopes";
  return scopes.join(", ");
}

function metadataPreview(metadata: Record<string, unknown>): string {
  const entries = Object.entries(metadata || {}).slice(0, 3);
  if (entries.length === 0) return "No metadata";
  return entries
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(" | ");
}

export function SettingsProjectDetailPage() {
  const navigate = useNavigate();
  const { projectId = "" } = useParams();

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [members, setMembers] = useState<Membership[]>([]);
  const [tokens, setTokens] = useState<TokenRow[]>([]);
  const [activity, setActivity] = useState<ActivityRow[]>([]);
  const [loading, setLoading] = useState(true);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameSubmitting, setRenameSubmitting] = useState(false);

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function refreshPage() {
    if (!projectId) {
      setProject(null);
      setMembers([]);
      setTokens([]);
      setActivity([]);
      setLoading(false);
      setError("No project identifier was provided.");
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const [detail, membershipRows, tokenRows, activityRows] = await Promise.all([
        apiFetch(`/settings/projects/${projectId}`),
        apiFetchAllPages<Membership>((cursor) => {
          const query = new URLSearchParams({ project_id: projectId, limit: "250" });
          if (cursor) query.set("cursor", cursor);
          return `/settings/rbac/project-memberships?${query.toString()}`;
        }),
        apiFetchAllPages<TokenRow>((cursor) => {
          const query = new URLSearchParams({ project_id: projectId, limit: "200" });
          if (cursor) query.set("cursor", cursor);
          return `/settings/api-tokens?${query.toString()}`;
        }),
        apiFetch(`/settings/audit?project_id=${projectId}&limit=100`),
      ]);

      setProject(detail as ProjectDetail);
      setMembers(membershipRows);
      setTokens(tokenRows);
      setActivity(((activityRows as { items?: ActivityRow[] })?.items || []) as ActivityRow[]);
      setRenameDraft((detail as ProjectDetail).name);
    } catch (err) {
      setProject(null);
      setMembers([]);
      setTokens([]);
      setActivity([]);
      setError(err instanceof Error ? err.message : "Failed to load project");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refreshPage().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function submitRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId || !project || renameSubmitting) return;
    setRenameSubmitting(true);
    setError(null);
    setInfo(null);
    try {
      const updated = (await apiFetch(`/settings/projects/${projectId}`, {
        method: "PATCH",
        body: JSON.stringify({ name: renameDraft.trim() }),
      })) as { name: string };
      setProject({ ...project, name: updated.name });
      setInfo("Project renamed.");
      setRenameOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rename project");
    } finally {
      setRenameSubmitting(false);
    }
  }

  async function confirmDelete() {
    if (!projectId || !project || deleteSubmitting) return;
    setDeleteSubmitting(true);
    setError(null);
    try {
      await apiFetch(`/settings/projects/${projectId}`, { method: "DELETE" });
      navigate("/settings/projects", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete project");
    } finally {
      setDeleteSubmitting(false);
    }
  }

  if (loading) {
    return <StatePanel title="Loading Project" description="Fetching project summary, members, tokens, and recent activity." />;
  }

  if (!project) {
    return <StatePanel title="Project Unavailable" description={error || "The requested project could not be loaded."} tone="error" />;
  }

  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <Link className="settings-meta underline" to="/settings/projects">
            Back to Projects
          </Link>
          <h2 className="settings-page-title mt-2">{project.name}</h2>
          <p className="settings-page-copy">Project-specific administration, including members, project-scoped tokens, recent activity, rename, and deletion.</p>
        </div>
        <div className="settings-toolbar">
          <button className="settings-button" onClick={() => refreshPage().catch(() => undefined)} type="button">
            Refresh
          </button>
          <button className="settings-button" onClick={() => setRenameOpen(true)} type="button">
            Rename
          </button>
          <button className="settings-button-danger" onClick={() => setDeleteOpen(true)} type="button">
            Delete
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
            <span className="settings-kpi-label">Members</span>
            <span className="settings-kpi-value">{project.member_count}</span>
            <p className="settings-kpi-copy">{project.admin_count} admins</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Tokens</span>
            <span className="settings-kpi-value">{project.active_token_count}</span>
            <p className="settings-kpi-copy">{project.token_count} total project tokens</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Runs</span>
            <span className="settings-kpi-value">{project.run_count}</span>
            <p className="settings-kpi-copy">{project.artifact_count} stored artifacts</p>
          </div>
        </div>

        <div className="mt-4 settings-badge-row">
          <span className="settings-badge settings-badge-neutral">Created {formatDateTime(project.created_at)}</span>
          <span className="settings-badge settings-badge-neutral">Last run {formatDateTime(project.last_run_at)}</span>
          {project.has_blocking_runs ? (
            <span className="settings-badge settings-badge-warning">{project.blocking_run_count} blocking runs</span>
          ) : (
            <span className="settings-badge settings-badge-positive">Delete-ready</span>
          )}
        </div>
      </section>

      {project.has_blocking_runs ? (
        <section className="settings-panel">
          <div className="settings-danger">
            <h3 className="settings-panel-title">Delete Blockers</h3>
            <p className="text-sm text-slate-700 dark:text-slate-300">
              This project cannot be deleted while runs are still in `UPLOADED` or `INGESTING`.
            </p>
            <div className="settings-table-wrap">
              <table className="settings-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {project.blocking_runs.map((run) => (
                    <tr key={run.id}>
                      <td>{run.name}</td>
                      <td>{run.status}</td>
                      <td>{formatDateTime(run.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      ) : null}

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Members</h3>
            <p className="settings-panel-copy">Current project access by user.</p>
          </div>
        </div>

        {members.length === 0 ? (
          <div className="mt-4 settings-empty">No project members were returned.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <tr key={member.user_id}>
                    <td>
                      <div className="font-semibold">{member.user_email}</div>
                      <div className="settings-meta">{member.user_id}</div>
                    </td>
                    <td>{member.role}</td>
                    <td className="text-right">
                      <Link className="settings-button" to={`/settings/users/${member.user_id}`}>
                        Open User
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Project Tokens</h3>
            <p className="settings-panel-copy">Project-scoped machine credentials and ownership.</p>
          </div>
        </div>

        {tokens.length === 0 ? (
          <div className="mt-4 settings-empty">No project-scoped tokens were returned.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Owner</th>
                  <th>Role</th>
                  <th>Scopes</th>
                  <th>Last used</th>
                  <th>Expires</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((token) => {
                  const status = tokenStatus(token);
                  return (
                    <tr key={token.id}>
                      <td>
                        <div className="font-semibold">{token.name}</div>
                        <div className="settings-meta">{token.id}</div>
                      </td>
                      <td>{token.user_email}</td>
                      <td>{token.role}</td>
                      <td className="settings-meta">{summarizeScopes(token.scopes)}</td>
                      <td>{formatDateTime(token.last_used_at)}</td>
                      <td>{formatDateTime(token.expires_at)}</td>
                      <td>
                        <span className={status.className}>{status.label}</span>
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
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Recent Activity</h3>
            <p className="settings-panel-copy">Recent project-scoped audit events.</p>
          </div>
        </div>

        {activity.length === 0 ? (
          <div className="mt-4 settings-empty">No recent project activity was returned.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {activity.map((event) => (
                  <tr key={event.id}>
                    <td>{formatDateTime(event.ts)}</td>
                    <td>{event.actor_email || event.actor_user_id || "system"}</td>
                    <td>{event.action}</td>
                    <td>
                      <div>{event.object_type}</div>
                      <div className="settings-meta">{event.object_id}</div>
                    </td>
                    <td className="settings-meta">{metadataPreview(event.metadata || {})}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Dialog
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        title="Rename project"
        description="Change the display name used across project pickers, audit views, and token inventory."
        footer={
          <>
            <button className="settings-button" onClick={() => setRenameOpen(false)} type="button">
              Cancel
            </button>
            <button className="settings-button-primary" form="rename-project-form" disabled={renameSubmitting || !renameDraft.trim()} type="submit">
              Save Name
            </button>
          </>
        }
      >
        <form className="settings-field" id="rename-project-form" onSubmit={submitRename}>
          <span className="settings-label">Project name</span>
          <input className="settings-input" value={renameDraft} onChange={(event) => setRenameDraft(event.target.value)} />
        </form>
      </Dialog>

      <Dialog
        open={deleteOpen}
        onClose={() => {
          setDeleteOpen(false);
          setDeleteConfirmation("");
        }}
        title="Delete project"
        description={`Type ${project.name} to confirm permanent deletion.`}
        footer={
          <>
            <button
              className="settings-button"
              onClick={() => {
                setDeleteOpen(false);
                setDeleteConfirmation("");
              }}
              type="button"
            >
              Cancel
            </button>
            <button
              className="settings-button-danger"
              disabled={deleteSubmitting || deleteConfirmation !== project.name || project.has_blocking_runs}
              onClick={() => confirmDelete().catch(() => undefined)}
              type="button"
            >
              Delete Project
            </button>
          </>
        }
      >
        <div className="grid gap-4">
          <div className="settings-danger">
            <p className="text-sm text-slate-700 dark:text-slate-300">
              This removes the project record, memberships, project-scoped tokens, runs, and stored artifacts associated with the project.
            </p>
            {project.has_blocking_runs ? (
              <p className="text-sm text-rose-700 dark:text-rose-200">Deletion is currently blocked because one or more runs are still in progress.</p>
            ) : null}
          </div>

          <label className="settings-field">
            <span className="settings-label">Confirm project name</span>
            <input
              className="settings-input"
              placeholder={project.name}
              value={deleteConfirmation}
              onChange={(event) => setDeleteConfirmation(event.target.value)}
            />
          </label>
        </div>
      </Dialog>
    </div>
  );
}
