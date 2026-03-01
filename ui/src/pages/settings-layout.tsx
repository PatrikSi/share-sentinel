import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type UserMe = { id: string; email: string; is_sysadmin: boolean };
type SettingsOutletContext = { me: UserMe };

const SETTINGS_TABS = [
  { to: "users", label: "Users" },
  { to: "rbac", label: "RBAC" },
  { to: "api-tokens", label: "API Tokens" },
  { to: "audit-logs", label: "Audit Logs" },
  { to: "security", label: "Security" },
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
          <h1 className="text-2xl font-bold">Setings</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">Loading settings…</p>
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="workspace">
        <div className="workspace-header">
          <h1 className="text-2xl font-bold">Setings</h1>
          <p className="rounded-xl bg-rose-100 p-3 text-sm text-rose-700 dark:bg-rose-900/30 dark:text-rose-200">{error}</p>
        </div>
      </section>
    );
  }

  if (!me?.is_sysadmin) {
    return (
      <section className="workspace">
        <div className="workspace-header">
          <h1 className="text-2xl font-bold">Setings</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">You need sysadmin access to view system settings.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="workspace">
      <div className="workspace-header">
        <h1 className="text-2xl font-bold">Setings</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">System-wide configuration, access control, and governance.</p>
        <nav className="flex flex-wrap gap-2 border-t border-[var(--app-border)] pt-3">
          {SETTINGS_TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                `rounded-lg border px-3 py-2 text-xs font-semibold uppercase tracking-wide ${
                  isActive
                    ? "border-emerald-600 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200"
                    : "border-slate-300 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                }`
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>
      </div>
      <Outlet context={{ me } satisfies SettingsOutletContext} />
    </section>
  );
}

export type { SettingsOutletContext };
