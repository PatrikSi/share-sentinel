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
    <>
      {showNav ? <TopNav /> : null}
      <main className="mx-auto max-w-7xl px-4 pb-8">
        <Routes>
          <Route path="/" element={<LoginPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId/runs/:runId" element={<RunDetailPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
