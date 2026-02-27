import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { TopNav } from "@/components/top-nav";
import { AdminPage } from "@/pages/admin-page";
import { LoginPage } from "@/pages/login-page";
import { ProjectsPage } from "@/pages/projects-page";
import { RunDetailPage } from "@/pages/run-detail-page";

export function App() {
  const location = useLocation();
  const showNav = location.pathname !== "/";
  const pageTitle = location.pathname.startsWith("/admin")
    ? "Administration"
    : location.pathname.startsWith("/projects/")
      ? "Run Explorer"
      : "Operations";

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
        {showNav ? (
          <header className="app-topbar">
            <h2 className="app-topbar-title">{pageTitle}</h2>
            <p className="app-topbar-subtitle">Share Sentinel Platform</p>
          </header>
        ) : null}
        <section className={showNav ? "app-content" : ""}>
        <Routes>
          <Route path="/" element={<LoginPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId/runs/:runId" element={<RunDetailPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </section>
      </main>
    </div>
  );
}
