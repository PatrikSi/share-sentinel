import { FormEvent, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { logoutSession, markSessionAnonymous, useSession } from "@/lib/auth";
import { useDashboardWorkspace } from "@/lib/dashboard-workspace";
import { ThemeToggle } from "@/components/theme-toggle";

export function TopNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useSession();
  const {
    canCreateProject,
    createProject,
    inProjectArea,
    projectLoadError,
    projectSectionLabel,
    projects,
    projectsReady,
    selectedProject,
    switchProject,
  } = useDashboardWorkspace();
  const [creatingProject, setCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  const [projectInfo, setProjectInfo] = useState<string | null>(null);
  const [showCreateProjectForm, setShowCreateProjectForm] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  async function logout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await logoutSession();
      markSessionAnonymous();
      navigate("/");
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : "Sign-out could not be confirmed. Please retry.");
    } finally {
      setLoggingOut(false);
    }
  }

  const navItems = [
    { to: "/projects", label: "Projects", match: "/projects" },
  ];
  if (session.user?.is_sysadmin) {
    navItems.push({ to: "/settings/users", label: "Settings", match: "/settings" });
  }
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
        <Link aria-label="Share Sentinel projects" className="app-nav-brand" to="/projects">
          <span aria-hidden="true" className="app-nav-mark">S</span>
          <span className="app-nav-title">Share Sentinel</span>
        </Link>

        <nav aria-label="Primary navigation" className="app-nav-links">
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
          <div className="app-project-context">
            <span className="app-project-section">{projectSectionLabel}</span>
            <select
              aria-label="Select active project"
              className="app-project-select"
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
              <div className="app-project-create">
                <button
                  aria-expanded={showCreateProjectForm}
                  aria-label="Create project"
                  onClick={() => {
                    setProjectError(null);
                    setProjectInfo(null);
                    setShowCreateProjectForm((prev) => !prev);
                  }}
                  title="Create project"
                  type="button"
                >
                  +
                </button>
                {showCreateProjectForm ? (
                  <form onSubmit={onCreateProject}>
                    <label>
                      Project name
                      <input
                        autoFocus
                        onChange={(event) => setNewProjectName(event.target.value)}
                        placeholder="Client East - Q1 Shares"
                        value={newProjectName}
                        required
                      />
                    </label>
                    <div>
                      <button onClick={() => setShowCreateProjectForm(false)} type="button">Cancel</button>
                      <button disabled={creatingProject} type="submit">{creatingProject ? "Creating…" : "Create"}</button>
                    </div>
                  </form>
                ) : null}
              </div>
            ) : null}
            {projectError || projectLoadError ? (
              <p className="app-project-message is-error" role="alert">{projectError || projectLoadError}</p>
            ) : null}
            {projectInfo ? <p className="app-project-message" role="status">{projectInfo}</p> : null}
          </div>
        ) : null}

        <div className="app-nav-actions">
          <ThemeToggle />
          <Link aria-current={location.pathname.startsWith("/account") ? "page" : undefined} className="app-account-link" to="/account">
            <span aria-hidden="true">{session.user?.email?.slice(0, 1).toUpperCase() || "A"}</span>
            <span>{session.user?.email || "Account"}</span>
          </Link>
          <button className="app-logout-btn" disabled={loggingOut} onClick={logout} type="button">
            {loggingOut ? "Signing out…" : logoutError ? "Retry sign out" : "Sign out"}
          </button>
        </div>
        {logoutError ? (
          <p className="app-logout-error" role="alert">
            <span>Sign-out was not confirmed. Your session is still active.</span>
            <span>{logoutError}</span>
          </p>
        ) : null}
      </div>
    </header>
  );
}
