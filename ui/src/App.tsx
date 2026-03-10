import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { TopNav } from "@/components/top-nav";
import { getAccessToken } from "@/lib/auth";
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

function RequireAuth({ children }: { children: JSX.Element }) {
  const location = useLocation();
  const token = getAccessToken();
  if (!token) {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={`/?next=${encodeURIComponent(next)}`} replace />;
  }
  return children;
}

export function App() {
  const location = useLocation();
  const showNav = location.pathname !== "/" && !!getAccessToken();

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

  return (
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
                  <RunDetailPage />
                </RequireAuth>
              }
            />
            <Route
              path="/projects/:projectId/inventory"
              element={
                <RequireAuth>
                  <ProjectInventoryPage />
                </RequireAuth>
              }
            />
            <Route
              path="/projects/:projectId/import"
              element={
                <RequireAuth>
                  <ProjectImportPage />
                </RequireAuth>
              }
            />
            <Route
              path="/admin"
              element={
                <RequireAuth>
                  <Navigate to="/settings/overview" replace />
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
              <Route index element={<Navigate to="/settings/overview" replace />} />
              <Route path="overview" element={<SettingsOverviewPage />} />
              <Route path="iam" element={<SettingsIamPage />} />
              <Route path="iam/users/:userId" element={<SettingsIamUserPage />} />
              <Route path="users" element={<Navigate to="/settings/iam" replace />} />
              <Route path="rbac" element={<Navigate to="/settings/iam" replace />} />
              <Route path="api-tokens" element={<SettingsApiTokensPage />} />
              <Route path="audit-logs" element={<SettingsAuditLogsPage />} />
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
}
