import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { StatePanel } from "@/components/state-panel";

type UserMe = { id: string; email: string; is_sysadmin: boolean };
type SettingsOutletContext = { me: UserMe };

const SETTINGS_TABS = [
  { to: "overview", label: "Overview", description: "Live posture and system signals" },
  { to: "iam", label: "Access", description: "Users, approvals, and project roles" },
  { to: "api-tokens", label: "Tokens", description: "Machine credentials and scopes" },
  { to: "audit-logs", label: "Audit", description: "Global activity and exports" },
];

export function SettingsLayout() {
  const [me, setMe] = useState<UserMe | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/auth/me")
      .then((data) => setMe(data as UserMe))
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load user context"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <section className="workspace">
        <div className="workspace-header">
          <h1 className="text-2xl font-bold">Settings</h1>
          <StatePanel title="Loading Settings" description="Fetching system-wide configuration, access, and governance context." />
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="workspace">
        <div className="workspace-header">
          <h1 className="text-2xl font-bold">Settings</h1>
          <StatePanel title="Settings Unavailable" description={error} tone="error" />
        </div>
      </section>
    );
  }

  if (!me?.is_sysadmin) {
    return (
      <section className="workspace">
        <div className="workspace-header">
          <h1 className="text-2xl font-bold">Settings</h1>
          <StatePanel
            title="Sysadmin Access Required"
            description="Only system administrators can view and change global settings."
            tone="warning"
          />
        </div>
      </section>
    );
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">System-wide configuration, access control, and governance.</p>
        <nav className="grid gap-3 border-t border-[var(--app-border)] pt-3 lg:grid-cols-4">
          {SETTINGS_TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                `rounded-3xl border px-4 py-3 ${
                  isActive
                    ? "border-emerald-600 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200"
                    : "border-slate-300 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                }`
              }
            >
              <p className="text-xs font-semibold uppercase tracking-[0.18em]">{tab.label}</p>
              <p className="mt-2 text-sm">{tab.description}</p>
            </NavLink>
          ))}
        </nav>
      </div>
      <Outlet context={{ me } satisfies SettingsOutletContext} />
    </section>
  );
}

export type { SettingsOutletContext };
