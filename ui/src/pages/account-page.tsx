import { FormEvent, useState } from "react";

import { Dialog } from "@/components/dialog";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";

export function AccountPage() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [confirmRevokeOpen, setConfirmRevokeOpen] = useState(false);

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setInfo(null);

    if (newPassword !== confirmPassword) {
      setError("New password and confirmation must match.");
      return;
    }

    setSaving(true);
    try {
      await apiFetch("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      setInfo("Password updated.");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update password");
    } finally {
      setSaving(false);
    }
  }

  async function revokeSessions() {
    setConfirmRevokeOpen(false);
    setError(null);
    setInfo(null);
    setRevoking(true);
    try {
      const data = await apiFetch("/auth/logout-all", { method: "POST" });
      const revoked = typeof data?.revoked_sessions === "number" ? data.revoked_sessions : 0;
      setInfo(`Revoked ${revoked} active session(s).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke sessions");
    } finally {
      setRevoking(false);
    }
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <h1 className="text-2xl font-bold">Account Security</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">Manage your credentials and active sessions.</p>
      </div>

      {error || info ? (
        <div className="workspace-section space-y-2">
          {error ? <StatusBanner tone="error" title="Account security change failed">{error}</StatusBanner> : null}
          {info ? <StatusBanner tone="success" title="Account security updated">{info}</StatusBanner> : null}
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 md:grid-cols-2">
        <div className="workspace-card">
          <h2 className="mb-3 text-lg font-semibold">Change Password</h2>
          <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">New passwords must satisfy the current server password policy.</p>
          <form className="space-y-3" onSubmit={changePassword}>
            <label className="block text-sm">
              Current password
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                autoComplete="current-password"
                type="password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              New password
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                autoComplete="new-password"
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              Confirm new password
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                autoComplete="new-password"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                required
              />
            </label>
            <button className="rounded-lg bg-pine px-3 py-1 text-sm font-semibold text-white" type="submit" disabled={saving}>
              {saving ? "Updating..." : "Update password"}
            </button>
          </form>
        </div>

        <div className="workspace-card space-y-3">
          <h2 className="text-lg font-semibold">Session Management</h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Revoke all refresh tokens tied to your account to force re-authentication everywhere.
          </p>
          <button
            className="rounded-lg bg-rose-700 px-3 py-2 text-sm font-semibold text-white transition hover:bg-rose-600 disabled:opacity-60"
            onClick={() => setConfirmRevokeOpen(true)}
            disabled={revoking}
            type="button"
          >
            {revoking ? "Revoking…" : "Revoke all sessions"}
          </button>
        </div>
      </div>

      <Dialog
        description="This invalidates every refresh session for your account across all browsers and devices. Active access may continue briefly until short-lived access tokens expire."
        footer={
          <>
            <button className="settings-button" onClick={() => setConfirmRevokeOpen(false)} type="button">
              Cancel
            </button>
            <button className="settings-button-danger" onClick={() => void revokeSessions()} type="button">
              Revoke all sessions
            </button>
          </>
        }
        onClose={() => setConfirmRevokeOpen(false)}
        open={confirmRevokeOpen}
        size="sm"
        title="Revoke every active session?"
      >
        <p className="text-sm text-slate-600 dark:text-slate-300">
          You may need to sign in again on this device. Passwords and API tokens are not changed by this action.
        </p>
      </Dialog>
    </section>
  );
}
