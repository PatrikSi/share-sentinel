import { useEffect, useMemo, useState } from "react";

import { apiFetch, apiFetchAllPages } from "@/lib/api";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";

type SecuritySettings = {
  allow_self_registration: boolean;
  auth_require_csrf: boolean;
  auth_cookie_secure: bool;
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

type UserRow = {
  id: string;
  email: string;
  is_active: boolean;
  is_sysadmin: boolean;
  is_approved: boolean;
};

type ApiTokenRow = {
  id: string;
  name: string;
  revoked_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
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

type ProjectRow = { id: string; name: string };

function formatTime(value: string | null): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "N/A";
  return parsed.toLocaleString();
}

export function SettingsOverviewPage() {
  const [security, setSecurity] = useState<SecuritySettings | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [tokens, setTokens] = useState<ApiTokenRow[]>([]);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [recentAudit, setRecentAudit] = useState<AuditEventRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadOverview() {
      setLoading(true);
      setError(null);
      try {
        const [securityData, userRows, tokenRows, projectRows, auditData] = await Promise.all([
          apiFetch("/auth/security-settings"),
          apiFetchAllPages<UserRow>((cursor) => {
            const query = new URLSearchParams({ limit: "200" });
            if (cursor) query.set("cursor", cursor);
            return `/users?${query.toString()}`;
          }),
          apiFetchAllPages<ApiTokenRow>((cursor) => {
            const query = new URLSearchParams({ limit: "200" });
            if (cursor) query.set("cursor", cursor);
            return `/settings/api-tokens?${query.toString()}`;
          }),
          apiFetch("/settings/projects"),
          apiFetch("/settings/audit?limit=5"),
        ]);

        if (cancelled) return;
        setSecurity(securityData as SecuritySettings);
        setUsers(userRows);
        setTokens(tokenRows);
        setProjects((projectRows || []) as ProjectRow[]);
        setRecentAudit(((auditData?.items as AuditEventRow[]) || []).slice(0, 5));
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load settings overview");
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

  const userStats = useMemo(() => {
    const total = users.length;
    const active = users.filter((user) => user.is_active).length;
    const pending = users.filter((user) => !user.is_approved).length;
    const sysadmins = users.filter((user) => user.is_sysadmin).length;
    return { total, active, pending, sysadmins };
  }, [users]);

  const tokenStats = useMemo(() => {
    const total = tokens.length;
    const active = tokens.filter((token) => !token.revoked_at).length;
    const revoked = total - active;
    const neverExpires = tokens.filter((token) => !token.revoked_at && !token.expires_at).length;
    return { total, active, revoked, neverExpires };
  }, [tokens]);

  const warnings = useMemo(() => {
    if (!security) return [];
    const nextWarnings: string[] = [];
    if (security.allow_self_registration) nextWarnings.push("Self-registration is enabled.");
    if (userStats.pending > 0) nextWarnings.push(`${userStats.pending} account(s) are waiting for approval.`);
    if (tokenStats.neverExpires > 0) nextWarnings.push(`${tokenStats.neverExpires} active token(s) never expire.`);
    if (!security.mfa_enabled) nextWarnings.push("MFA is not enabled.");
    if (!security.sso_enabled) nextWarnings.push("SSO is not enabled.");
    return nextWarnings;
  }, [security, tokenStats.neverExpires, userStats.pending]);

  if (loading) {
    return (
      <div className="workspace-section">
        <StatePanel
          title="Loading Overview"
          description="Pulling live security posture, token inventory, and recent audit activity."
        />
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

  if (!security) {
    return (
      <div className="workspace-section">
        <StatePanel title="No Overview Data" description="Security posture data was not returned by the API." tone="warning" />
      </div>
    );
  }

  return (
    <div className="workspace-section space-y-4">
      {warnings.length > 0 ? (
        <StatusBanner tone="warning" title="Attention">
          <div className="space-y-1">
            {warnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </StatusBanner>
      ) : (
        <StatusBanner tone="success" title="Posture">
          <p>No immediate governance warnings were detected in the current settings snapshot.</p>
        </StatusBanner>
      )}

      <div className="grid gap-4 xl:grid-cols-4">
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Registration</p>
          <p className="mt-2 text-3xl font-semibold">{security.allow_self_registration ? "Open" : "Approval only"}</p>
          <p className="mt-2 text-sm text-slate-500">Self-registration is {security.allow_self_registration ? "enabled" : "disabled"}.</p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Identity Directory</p>
          <p className="mt-2 text-3xl font-semibold">{userStats.total}</p>
          <p className="mt-2 text-sm text-slate-500">
            {userStats.active} active, {userStats.pending} pending, {userStats.sysadmins} sysadmins.
          </p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">API Tokens</p>
          <p className="mt-2 text-3xl font-semibold">{tokenStats.active}</p>
          <p className="mt-2 text-sm text-slate-500">
            {tokenStats.revoked} revoked, {tokenStats.neverExpires} never expire.
          </p>
        </section>
        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Projects</p>
          <p className="mt-2 text-3xl font-semibold">{projects.length}</p>
          <p className="mt-2 text-sm text-slate-500">Projects currently available for access control and token scoping.</p>
        </section>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_360px]">
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

        <section className="workspace-card">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Access Posture</p>
          <h2 className="mt-2 text-xl font-semibold">Security capabilities</h2>
          <div className="mt-4 space-y-3 text-sm">
            <div className="flex items-center justify-between gap-3">
              <span>CSRF protection</span>
              <span className="font-semibold">{security.auth_require_csrf ? "Required" : "Off"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>Secure auth cookies</span>
              <span className="font-semibold">{security.auth_cookie_secure ? "Enabled" : "Off"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>RBAC</span>
              <span className="font-semibold">{security.rbac_enabled ? "Enabled" : "Off"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>MFA</span>
              <span className="font-semibold">{security.mfa_enabled ? "Enabled" : "Planned"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>SSO</span>
              <span className="font-semibold">{security.sso_enabled ? "Enabled" : "Planned"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>SCIM</span>
              <span className="font-semibold">{security.scim_enabled ? "Enabled" : "Planned"}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span>Password history</span>
              <span className="font-semibold">{security.password_history_enforced ? "Enabled" : "Not enforced"}</span>
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
              <p className="mt-1 text-sm font-semibold">{tokenStats.neverExpires}</p>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Last active token use</p>
              <p className="mt-1 text-sm font-semibold">
                {formatTime(
                  tokens
                    .filter((token) => !token.revoked_at && token.last_used_at)
                    .sort((a, b) => new Date(b.last_used_at || 0).getTime() - new Date(a.last_used_at || 0).getTime())[0]?.last_used_at || null,
                )}
              </p>
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
