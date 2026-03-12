import { useEffect, useState } from "react";

import { StatePanel } from "@/components/state-panel";
import { apiFetch } from "@/lib/api";

type SecuritySettings = {
  allow_self_registration: boolean;
  auth_require_csrf: boolean;
  auth_cookie_secure: boolean;
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
  auth_login_max_attempts: number;
  auth_login_window_seconds: number;
  auth_login_lockout_seconds: number;
  default_api_token_expiry_days: number;
  rbac_enabled: boolean;
  mfa_enabled: boolean;
  sso_enabled: boolean;
  scim_enabled: boolean;
  password_history_enforced: boolean;
  session_idle_timeout_minutes: number | null;
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
          setError(err instanceof Error ? err.message : "Failed to load settings overview");
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
    return (
      <div className="workspace-section">
        <StatePanel title="Loading Overview" description="Pulling live security posture, token inventory, and recent audit activity." />
      </div>
    );
  }

  if (error) {
    return (
      <div className="workspace-section">
        <StatePanel title="Overview Unavailable" description={error} tone="error" />
      </div>
    );
  }

  if (!overview) {
    return (
      <div className="workspace-section">
        <StatePanel title="No Overview Data" description="Security posture data was not returned by the API." tone="warning" />
      </div>
    );
  }

  const { security, users, tokens, projects, recent_audit: recentAudit } = overview;

  return (
    <div className="workspace-section space-y-4">
      <div className="grid gap-4 xl:grid-cols-4">
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Registration</p>
          <p className="mt-2 text-3xl font-semibold">{security.allow_self_registration ? "Open" : "Approval only"}</p>
          <p className="mt-2 text-sm text-slate-500">Self-registration is {security.allow_self_registration ? "enabled" : "disabled"}.</p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Identity Directory</p>
          <p className="mt-2 text-3xl font-semibold">{users.total}</p>
          <p className="mt-2 text-sm text-slate-500">
            {users.active} active, {users.pending} pending, {users.sysadmins} sysadmins.
          </p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">API Tokens</p>
          <p className="mt-2 text-3xl font-semibold">{tokens.active}</p>
          <p className="mt-2 text-sm text-slate-500">
            {tokens.revoked} revoked, {tokens.never_expires} never expire.
          </p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Projects</p>
          <p className="mt-2 text-3xl font-semibold">{projects.total}</p>
          <p className="mt-2 text-sm text-slate-500">Projects currently available for access control and token scoping.</p>
        </section>
      </div>

      <div className="grid gap-4">
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Password Policy</p>
          <h2 className="mt-2 text-xl font-semibold">Current requirements</h2>
          <div className="mt-4 flex flex-wrap gap-2 text-xs">
            <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">Min length: {security.password_min_length}</span>
            {security.password_require_lowercase ? <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">Lowercase required</span> : null}
            {security.password_require_uppercase ? <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">Uppercase required</span> : null}
            {security.password_require_number ? <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">Number required</span> : null}
            {security.password_require_special ? <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">Special char required</span> : null}
          </div>
          <div className="mt-5 grid gap-3 md:grid-cols-3">
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Login guardrail</p>
              <p className="mt-1 text-sm font-semibold">{security.auth_login_max_attempts} attempts</p>
              <p className="mt-1 text-xs text-slate-500">Within {security.auth_login_window_seconds} seconds.</p>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Lockout</p>
              <p className="mt-1 text-sm font-semibold">{security.auth_login_lockout_seconds} seconds</p>
              <p className="mt-1 text-xs text-slate-500">Temporary lockout after repeated failures.</p>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Session timeout</p>
              <p className="mt-1 text-sm font-semibold">{security.session_idle_timeout_minutes ? `${security.session_idle_timeout_minutes} min` : "Not enforced"}</p>
              <p className="mt-1 text-xs text-slate-500">Idle session expiration.</p>
            </div>
          </div>
        </section>
      </div>

      <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Token Hygiene</p>
          <h2 className="mt-2 text-xl font-semibold">Credential posture</h2>
          <div className="mt-4 space-y-3">
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Default expiry</p>
              <p className="mt-1 text-sm font-semibold">{security.default_api_token_expiry_days} days</p>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Never-expiring tokens</p>
              <p className="mt-1 text-sm font-semibold">{tokens.never_expires}</p>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Last active token use</p>
              <p className="mt-1 text-sm font-semibold">{formatTime(tokens.last_active_at)}</p>
            </div>
          </div>
        </section>

        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Recent Audit</p>
          <h2 className="mt-2 text-xl font-semibold">Latest global events</h2>
          {recentAudit.length === 0 ? (
            <div className="mt-4">
              <StatePanel title="No Audit Events" description="No recent global audit events were returned by the API." />
            </div>
          ) : (
            <div className="mt-4 space-y-3">
              {recentAudit.map((event) => (
                <article className="rounded-2xl border border-slate-200 p-4 dark:border-slate-800" key={event.id}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{event.action}</p>
                      <p className="mt-1 text-sm font-semibold">
                        {event.object_type}: {event.object_id}
                      </p>
                    </div>
                    <p className="text-xs text-slate-500">{formatTime(event.ts)}</p>
                  </div>
                  <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                    Actor: {event.actor_email || "system"} {event.project_name ? `• Project: ${event.project_name}` : "• Global"}
                  </p>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
