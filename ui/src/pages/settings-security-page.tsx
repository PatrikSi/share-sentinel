import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";

type SecuritySettings = {
  allow_self_registration: boolean;
  auth_require_csrf: boolean;
  auth_cookie_secure: boolean;
  password_min_length: number;
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

function boolLabel(value: boolean): string {
  return value ? "Enabled" : "Disabled";
}

export function SettingsSecurityPage() {
  const [settings, setSettings] = useState<SecuritySettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/auth/security-settings")
      .then((data) => setSettings(data as SecuritySettings))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load security settings"));
  }, []);

  const missingControls = useMemo(() => {
    if (!settings) return [];
    const controls: string[] = [];
    if (!settings.mfa_enabled) controls.push("MFA");
    if (!settings.sso_enabled) controls.push("SSO");
    if (!settings.scim_enabled) controls.push("SCIM provisioning");
    if (!settings.password_history_enforced) controls.push("Password history policy");
    if (!settings.session_idle_timeout_minutes) controls.push("Idle session timeout");
    return controls;
  }, [settings]);

  return (
    <>
      {error ? (
        <div className="workspace-section">
          <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p>
        </div>
      ) : null}

      {!settings ? (
        <div className="workspace-section">
          <p className="text-sm text-slate-500">Loading security controls…</p>
        </div>
      ) : (
        <div className="workspace-section grid gap-4 lg:grid-cols-2">
          <div className="workspace-card space-y-2">
            <h2 className="text-lg font-semibold">Authentication Controls</h2>
            <p className="text-sm">Self-registration: {boolLabel(settings.allow_self_registration)}</p>
            <p className="text-sm">CSRF required: {boolLabel(settings.auth_require_csrf)}</p>
            <p className="text-sm">Secure auth cookie: {boolLabel(settings.auth_cookie_secure)}</p>
            <p className="text-sm">Password minimum length: {settings.password_min_length}</p>
            <p className="text-sm">
              Login throttle: {settings.auth_login_max_attempts} attempts in {settings.auth_login_window_seconds}s, lockout{" "}
              {settings.auth_login_lockout_seconds}s
            </p>
            <p className="text-sm">Default API token expiry: {settings.default_api_token_expiry_days} days</p>
          </div>

          <div className="workspace-card space-y-2">
            <h2 className="text-lg font-semibold">Enterprise Security Flags</h2>
            <p className="text-sm">RBAC: {boolLabel(settings.rbac_enabled)}</p>
            <p className="text-sm">MFA: {boolLabel(settings.mfa_enabled)}</p>
            <p className="text-sm">SSO: {boolLabel(settings.sso_enabled)}</p>
            <p className="text-sm">SCIM provisioning: {boolLabel(settings.scim_enabled)}</p>
            <p className="text-sm">Password history policy: {boolLabel(settings.password_history_enforced)}</p>
            <p className="text-sm">
              Session idle timeout: {settings.session_idle_timeout_minutes ? `${settings.session_idle_timeout_minutes} minutes` : "Not configured"}
            </p>
          </div>

          <div className="workspace-card lg:col-span-2">
            <h2 className="text-lg font-semibold">Gaps To Implement</h2>
            {missingControls.length === 0 ? (
              <p className="text-sm text-emerald-700 dark:text-emerald-300">No missing controls in this checklist.</p>
            ) : (
              <ul className="list-disc pl-5 text-sm">
                {missingControls.map((control) => (
                  <li key={control}>{control}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </>
  );
}
