import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { TopNav } from "@/components/top-nav";
import { getAccessToken } from "@/lib/auth";
import { AdminPage } from "@/pages/admin-page";
import { LoginPage } from "@/pages/login-page";
import { ProjectsPage } from "@/pages/projects-page";
import { RunDetailPage } from "@/pages/run-detail-page";

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
              path="/admin"
              element={
                <RequireAuth>
                  <AdminPage />
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
