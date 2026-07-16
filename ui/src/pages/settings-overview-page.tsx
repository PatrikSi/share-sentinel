import { Link } from "react-router-dom";
import { useEffect, useState } from "react";

import { StatePanel } from "@/components/state-panel";
import { apiFetch } from "@/lib/api";

type SecuritySettings = {
  allow_self_registration: boolean;
  auth_require_csrf: boolean;
  auth_cookie_secure: boolean;
  allow_never_expiring_api_tokens: boolean;
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
  auth_login_max_attempts: number;
  auth_login_window_seconds: number;
  auth_login_lockout_seconds: number;
  default_api_token_expiry_days: number;
};

type AuditEventRow = {
  id: number;
  ts: string;
  actor_email: string | null;
  action: string;
  object_type: string;
  object_id: string;
  project_name: string | null;
};

type OverviewPayload = {
  security: SecuritySettings;
  users: {
    total: number;
    active: number;
    pending: number;
    sysadmins: number;
  };
  tokens: {
    total: number;
    active: number;
    revoked: number;
    never_expires: number;
    last_active_at: string | null;
  };
  projects: {
    total: number;
  };
  recent_audit: AuditEventRow[];
};

function formatTime(value: string | null): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "N/A";
  return parsed.toLocaleString();
}

function passwordPolicySummary(security: SecuritySettings): string {
  const parts = [`Minimum ${security.password_min_length} characters`];
  if (security.password_require_lowercase) parts.push("lowercase");
  if (security.password_require_uppercase) parts.push("uppercase");
  if (security.password_require_number) parts.push("number");
  if (security.password_require_special) parts.push("special character");
  return parts.join(", ");
}

export function SettingsOverviewPage() {
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadOverview() {
      setLoading(true);
      setError(null);
      try {
        const data = (await apiFetch("/settings/overview")) as OverviewPayload;
        if (!cancelled) {
          setOverview(data);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load administration summary");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    loadOverview().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return <StatePanel title="Loading Administration Summary" description="Collecting current user, token, and policy state." />;
  }

  if (error) {
    return <StatePanel title="Administration Summary Unavailable" description={error} tone="error" />;
  }

  if (!overview) {
    return <StatePanel title="No Administration Summary" description="The API did not return summary data." tone="warning" />;
  }

  const { security, users, tokens, projects, recent_audit: recentAudit } = overview;

  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <h2 className="settings-page-title">General</h2>
          <p className="settings-page-copy">A small admin home for the current system state and the next places to work.</p>
        </div>
      </div>

      <div className="settings-grid-2">
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Attention</h3>
              <p className="settings-panel-copy">Jump directly into the areas that usually need action.</p>
            </div>
          </div>

          <div className="mt-4 grid gap-3">
            <div className="settings-kpi">
              <span className="settings-kpi-label">Pending approvals</span>
              <span className="settings-kpi-value">{users.pending}</span>
              <p className="settings-kpi-copy">
                Review account approvals in <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/users">Users</Link>.
              </p>
            </div>
            <div className="settings-kpi">
              <span className="settings-kpi-label">Projects</span>
              <span className="settings-kpi-value">{projects.total}</span>
              <p className="settings-kpi-copy">
                Manage project ownership and cleanup in{" "}
                <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/projects">
                  Projects
                </Link>
                .
              </p>
            </div>
            <div className="settings-kpi">
              <span className="settings-kpi-label">Never-expiring tokens</span>
              <span className="settings-kpi-value">{tokens.never_expires}</span>
              <p className="settings-kpi-copy">
                Review credential risk in{" "}
                <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/tokens">
                  API Tokens
                </Link>
                .
              </p>
            </div>
          </div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Policy Snapshot</h3>
              <p className="settings-panel-copy">Read-only platform controls that affect user and token handling.</p>
            </div>
          </div>

          <dl className="mt-4 grid gap-3 text-sm">
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Registration</dt>
              <dd>{security.allow_self_registration ? "Self-registration enabled" : "Approval required"}</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Password policy</dt>
              <dd className="max-w-[32rem] text-right">{passwordPolicySummary(security)}</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Login throttling</dt>
              <dd>
                {security.auth_login_max_attempts} attempts in {security.auth_login_window_seconds}s, lock for{" "}
                {security.auth_login_lockout_seconds}s
              </dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Default token expiry</dt>
              <dd>{security.default_api_token_expiry_days} days</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Never-expiring tokens</dt>
              <dd>{security.allow_never_expiring_api_tokens ? "Allowed" : "Blocked"}</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">CSRF protection</dt>
              <dd>{security.auth_require_csrf ? "Required" : "Disabled"}</dd>
            </div>
            <div className="flex items-start justify-between gap-3">
              <dt className="text-slate-500 dark:text-slate-400">Secure auth cookies</dt>
              <dd>{security.auth_cookie_secure ? "Enabled" : "Disabled"}</dd>
            </div>
          </dl>
        </section>
      </div>

      <div className="settings-grid-3">
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Users</h3>
              <p className="settings-panel-copy">Current directory footprint.</p>
            </div>
          </div>
          <div className="mt-4 grid gap-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Total users</span>
              <span>{users.total}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Active</span>
              <span>{users.active}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Sysadmins</span>
              <span>{users.sysadmins}</span>
            </div>
          </div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Tokens</h3>
              <p className="settings-panel-copy">Machine credential posture.</p>
            </div>
          </div>
          <div className="mt-4 grid gap-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Active</span>
              <span>{tokens.active}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Revoked</span>
              <span>{tokens.revoked}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-slate-500 dark:text-slate-400">Last activity</span>
              <span>{formatTime(tokens.last_active_at)}</span>
            </div>
          </div>
        </section>

        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Next Steps</h3>
              <p className="settings-panel-copy">The main admin surfaces linked from one place.</p>
            </div>
          </div>
          <ul className="mt-4 settings-note-list">
            <li>
              <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/users">
                Users
              </Link>
              : approvals, lifecycle, and project access.
            </li>
            <li>
              <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/projects">
                Projects
              </Link>
              : ownership, members, and deletion.
            </li>
            <li>
              <Link className="font-semibold text-slate-900 underline dark:text-slate-100" to="/settings/audit">
                Audit Log
              </Link>
              : privileged activity and export.
            </li>
          </ul>
        </section>
      </div>

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Recent Administrative Activity</h3>
            <p className="settings-panel-copy">Recent cross-system events for fast orientation, with full review in the audit log.</p>
          </div>
          <Link className="settings-button" to="/settings/audit">
            Open Audit Log
          </Link>
        </div>

        {recentAudit.length === 0 ? (
          <div className="mt-4 settings-empty">No recent audit events were returned.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Action</th>
                  <th>Object</th>
                  <th>Actor</th>
                  <th>Scope</th>
                </tr>
              </thead>
              <tbody>
                {recentAudit.map((event) => (
                  <tr key={event.id}>
                    <td>{formatTime(event.ts)}</td>
                    <td>{event.action}</td>
                    <td>
                      <div>{event.object_type}</div>
                      <div className="settings-meta">{event.object_id}</div>
                    </td>
                    <td>{event.actor_email || "system"}</td>
                    <td>{event.project_name || "Global"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
