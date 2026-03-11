import { FormEvent, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { clearTokens, getRefreshToken } from "@/lib/auth";
import { useDashboardWorkspace } from "@/lib/dashboard-workspace";
import { ThemeToggle } from "@/components/theme-toggle";

export function TopNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";
  const {
    canCreateProject,
    createProject,
    inProjectArea,
    projectLoadError,
    projectSectionLabel,
    projects,
    projectsReady,
    selectedProject,
    selectedProjectName,
    switchProject,
  } = useDashboardWorkspace();
  const [creatingProject, setCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  const [projectInfo, setProjectInfo] = useState<string | null>(null);
  const [showCreateProjectForm, setShowCreateProjectForm] = useState(false);

  async function logout() {
    const refreshToken = getRefreshToken();
    if (refreshToken) {
      try {
        await fetch(`${API_BASE}/auth/logout`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      } catch {
        // Local cleanup still proceeds.
      }
    }
    clearTokens();
    navigate("/");
  }

  const navItems = [
    { to: "/projects", label: "Dashboard", match: "/projects" },
    { to: "/account", label: "Account", match: "/account" },
    { to: "/settings/users", label: "Settings", match: "/settings" },
  ];
  const showDashboardControls = inProjectArea;

  async function onCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newProjectName.trim()) return;
    setCreatingProject(true);
    setProjectError(null);
    setProjectInfo(null);

    try {
      const created = await createProject(newProjectName);
      setNewProjectName("");
      setShowCreateProjectForm(false);
      setProjectInfo(`Project created: ${created.name}`);
    } catch (err) {
      setProjectError(err instanceof Error ? err.message : "Project creation failed");
    } finally {
      setCreatingProject(false);
    }
  }

  return (
    <header className="app-nav">
      <div className="app-nav-inner">
        <Link className="app-nav-brand" to="/projects">
          <span className="app-nav-title">share-sentinel</span>
        </Link>

        <nav className="app-nav-links">
          {navItems.map((item) => (
            <Link
              className={`app-nav-link ${location.pathname.startsWith(item.match) ? "is-active" : ""}`}
              key={item.to}
              to={item.to}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        {showDashboardControls ? (
          <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-slate-300 bg-slate-50/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900/70">
            <div className="basis-full flex flex-wrap items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">
              <Link className="hover:text-slate-700 dark:hover:text-slate-200" to="/projects">
                Dashboard
              </Link>
              {selectedProjectName ? (
                <>
                  <span>/</span>
                  <span className="text-slate-700 dark:text-slate-200">{selectedProjectName}</span>
                </>
              ) : null}
              {projectSectionLabel !== "Dashboard" ? (
                <>
                  <span>/</span>
                  <span className="text-slate-700 dark:text-slate-200">{projectSectionLabel}</span>
                </>
              ) : null}
            </div>
            <select
              className="min-w-[220px] rounded-xl border border-slate-300 bg-white/90 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
              disabled={!projectsReady || projects.length === 0}
              value={selectedProject}
              onChange={(event) => {
                setProjectError(null);
                setProjectInfo(null);
                switchProject(event.target.value);
              }}
            >
              {!projectsReady ? <option value="">Loading projects...</option> : null}
              {projectsReady && projects.length === 0 ? <option value="">No projects available</option> : null}
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            {canCreateProject ? (
              <button
                className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                onClick={() => {
                  setProjectError(null);
                  setProjectInfo(null);
                  setShowCreateProjectForm((prev) => !prev);
                }}
                type="button"
              >
                  {showCreateProjectForm ? "Close" : "New Project"}
                </button>
              ) : null}
            {canCreateProject && showCreateProjectForm ? (
              <form className="flex flex-wrap items-center gap-2" onSubmit={onCreateProject}>
                <input
                  className="min-w-[220px] rounded-xl border border-slate-300 bg-white/90 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
                  onChange={(event) => setNewProjectName(event.target.value)}
                  placeholder="Client East - Q1 Shares"
                  value={newProjectName}
                  required
                />
                <button
                  className="rounded-xl bg-slate-900 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                  disabled={creatingProject}
                  type="submit"
                >
                  {creatingProject ? "Creating..." : "Create"}
                </button>
              </form>
            ) : null}
            {projectError || projectLoadError ? (
              <p className="basis-full text-xs text-rose-600 dark:text-rose-300">{projectError || projectLoadError}</p>
            ) : null}
            {projectInfo ? <p className="basis-full text-xs text-emerald-700 dark:text-emerald-300">{projectInfo}</p> : null}
          </div>
        ) : null}

        <div className="app-nav-actions">
          <ThemeToggle />
          <button className="app-logout-btn" onClick={logout}>
            Logout
          </button>
        </div>
      </div>
    </header>
  );
}
