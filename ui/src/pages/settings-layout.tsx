import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { StatePanel } from "@/components/state-panel";

type UserMe = { id: string; email: string; is_sysadmin: boolean };
type SettingsOutletContext = { me: UserMe };

const SETTINGS_SECTIONS = [
  { to: "general", label: "General", description: "Admin summary and policy snapshot" },
  { to: "users", label: "Users", description: "Identity lifecycle and access" },
  { to: "projects", label: "Projects", description: "Project ownership and cleanup" },
  { to: "tokens", label: "API Tokens", description: "Machine credential inventory" },
  { to: "audit", label: "Audit Log", description: "Privileged activity and exports" },
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
      <section className="space-y-4">
        <div className="settings-page-header">
          <div>
            <h1 className="settings-page-title">Settings</h1>
            <p className="settings-page-copy">Loading the administration workspace.</p>
          </div>
        </div>
        <StatePanel title="Loading Settings" description="Fetching system-wide user, project, and security context." />
      </section>
    );
  }

  if (error) {
    return (
      <section className="space-y-4">
        <div className="settings-page-header">
          <div>
            <h1 className="settings-page-title">Settings</h1>
            <p className="settings-page-copy">Administration is currently unavailable.</p>
          </div>
        </div>
        <StatePanel title="Settings Unavailable" description={error} tone="error" />
      </section>
    );
  }

  if (!me?.is_sysadmin) {
    return (
      <section className="space-y-4">
        <div className="settings-page-header">
          <div>
            <h1 className="settings-page-title">Settings</h1>
            <p className="settings-page-copy">This area is reserved for system administrators.</p>
          </div>
        </div>
        <StatePanel
          title="Sysadmin Access Required"
          description="Only system administrators can view and change global administration settings."
          tone="warning"
        />
      </section>
    );
  }

  return (
    <section className="space-y-5">
      <div className="settings-page-header">
        <div>
          <h1 className="settings-page-title">Settings</h1>
          <p className="settings-page-copy">A single workspace for users, projects, tokens, and audit review.</p>
        </div>
      </div>

      <div className="settings-layout">
        <aside className="settings-sidebar">
          <div className="settings-sidebar-header">
            <p className="settings-sidebar-title">Administration</p>
            <p className="settings-sidebar-copy">{me.email}</p>
          </div>
          <nav className="settings-nav">
            {SETTINGS_SECTIONS.map((section) => (
              <NavLink
                key={section.to}
                to={section.to}
                className={({ isActive }) => `settings-nav-link${isActive ? " is-active" : ""}`}
              >
                <span className="settings-nav-label">{section.label}</span>
                <span className="settings-nav-description">{section.description}</span>
              </NavLink>
            ))}
          </nav>
        </aside>

        <main className="settings-page">
          <Outlet context={{ me } satisfies SettingsOutletContext} />
        </main>
      </div>
    </section>
  );
}

export type { SettingsOutletContext };
