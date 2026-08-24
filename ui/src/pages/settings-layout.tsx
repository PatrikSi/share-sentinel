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
  const [retryNonce, setRetryNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    apiFetch("/auth/me", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setMe(data as UserMe);
      })
      .catch((err) => {
        if (!controller.signal.aborted && !(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Failed to load user context");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [retryNonce]);

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
        <StatePanel
          actions={
            <button className="settings-button" onClick={() => setRetryNonce((current) => current + 1)} type="button">
              Retry settings access
            </button>
          }
          title="Settings Unavailable"
          description={`${error} No settings were changed; retrying this read is safe.`}
          tone="error"
        />
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
          <nav aria-label="Settings sections" className="settings-nav">
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

        <div className="settings-page">
          <Outlet context={{ me } satisfies SettingsOutletContext} />
        </div>
      </div>
    </section>
  );
}

export type { SettingsOutletContext };
