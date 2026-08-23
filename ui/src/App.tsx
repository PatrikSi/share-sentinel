import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { TopNav } from "@/components/top-nav";
import { bootstrapSession, resetSession, useSession } from "@/lib/auth";
import { DashboardWorkspaceProvider } from "@/lib/dashboard-workspace";
import { AccountPage } from "@/pages/account-page";
import { LoginPage } from "@/pages/login-page";
import { ProjectImportPage } from "@/pages/project-import-page";
import { ProjectInventoryPage } from "@/pages/project-inventory-page";
import { ProjectsPage } from "@/pages/projects-page";
import { RunDetailPage } from "@/pages/run-detail-page";
import { SettingsApiTokensPage } from "@/pages/settings-api-tokens-page";
import { SettingsAuditLogsPage } from "@/pages/settings-audit-logs-page";
import { SettingsIamPage } from "@/pages/settings-iam-page";
import { SettingsIamUserPage } from "@/pages/settings-iam-user-page";
import { SettingsLayout } from "@/pages/settings-layout";
import { SettingsOverviewPage } from "@/pages/settings-overview-page";
import { SettingsProjectDetailPage } from "@/pages/settings-project-detail-page";
import { SettingsProjectsPage } from "@/pages/settings-projects-page";

function RequireAuth({ children }: { children: JSX.Element }) {
  const location = useLocation();
  const session = useSession();

  if (session.status === "unknown") {
    return (
      <section className="mx-auto mt-16 max-w-md">
        <StatePanel title="Checking Session" description="Validating the current browser session." />
      </section>
    );
  }

  if (session.status === "error") {
    return (
      <section className="mx-auto mt-16 max-w-md">
        <StatePanel
          actions={
            <button className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-semibold text-white hover:bg-emerald-600" onClick={resetSession} type="button">
              Retry session check
            </button>
          }
          description={`${session.error || "The authentication service could not be reached."} Your login state has not been changed.`}
          title="Session Check Unavailable"
          tone="error"
        />
      </section>
    );
  }

  if (session.status !== "authenticated") {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={`/?next=${encodeURIComponent(next)}`} replace />;
  }
  return children;
}

function RunDetailRoute() {
  const { projectId, runId } = useParams<{ projectId: string; runId: string }>();
  return <RunDetailPage key={`${projectId || ""}:${runId || ""}`} />;
}

function ProjectInventoryRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  return <ProjectInventoryPage key={projectId || ""} />;
}

function ProjectImportRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  return <ProjectImportPage key={projectId || ""} />;
}

export function App() {
  const location = useLocation();
  const session = useSession();
  const showNav = location.pathname !== "/" && session.status === "authenticated";

  useEffect(() => {
    if (session.status === "unknown") {
      void bootstrapSession();
    }
  }, [session.status]);

  useEffect(() => {
    const saved = localStorage.getItem("share_sentinel_theme") || "system";
    const root = document.documentElement;
    if (saved === "system") {
      const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.classList.toggle("dark", dark);
    } else {
      root.classList.toggle("dark", saved === "dark");
    }
  }, []);

  const appShell = (
    <div className={showNav ? "app-shell" : ""}>
      {showNav ? <TopNav /> : null}
      <main className={showNav ? "app-main" : "app-login-main"}>
        <section className={showNav ? "app-content" : ""}>
          <Routes>
            <Route path="/" element={<LoginPage />} />
            <Route
              path="/projects"
              element={
                <RequireAuth>
                  <ProjectsPage />
                </RequireAuth>
              }
            />
            <Route
              path="/projects/:projectId/runs/:runId"
              element={
                <RequireAuth>
                  <RunDetailRoute />
                </RequireAuth>
              }
            />
            <Route
              path="/projects/:projectId/inventory"
              element={
                <RequireAuth>
                  <ProjectInventoryRoute />
                </RequireAuth>
              }
            />
            <Route
              path="/projects/:projectId/import"
              element={
                <RequireAuth>
                  <ProjectImportRoute />
                </RequireAuth>
              }
            />
            <Route
              path="/admin"
              element={
                <RequireAuth>
                  <Navigate to="/settings/users" replace />
                </RequireAuth>
              }
            />
            <Route
              path="/settings"
              element={
                <RequireAuth>
                  <SettingsLayout />
                </RequireAuth>
              }
            >
              <Route index element={<Navigate to="/settings/users" replace />} />
              <Route path="general" element={<SettingsOverviewPage />} />
              <Route path="users" element={<SettingsIamPage />} />
              <Route path="users/:userId" element={<SettingsIamUserPage />} />
              <Route path="projects" element={<SettingsProjectsPage />} />
              <Route path="projects/:projectId" element={<SettingsProjectDetailPage />} />
              <Route path="tokens" element={<SettingsApiTokensPage />} />
              <Route path="audit" element={<SettingsAuditLogsPage />} />
              <Route path="overview" element={<Navigate to="/settings/general" replace />} />
              <Route path="iam" element={<Navigate to="/settings/users" replace />} />
              <Route path="iam/users/:userId" element={<SettingsIamUserPage />} />
              <Route path="rbac" element={<Navigate to="/settings/users" replace />} />
              <Route path="api-tokens" element={<Navigate to="/settings/tokens" replace />} />
              <Route path="audit-logs" element={<Navigate to="/settings/audit" replace />} />
            </Route>
            <Route
              path="/account"
              element={
                <RequireAuth>
                  <AccountPage />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </section>
      </main>
    </div>
  );

  if (!showNav) {
    return appShell;
  }

  return <DashboardWorkspaceProvider>{appShell}</DashboardWorkspaceProvider>;
}
