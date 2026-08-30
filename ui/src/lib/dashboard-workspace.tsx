import { createContext, ReactNode, useContext, useEffect, useState } from "react";
import { matchPath, useLocation, useNavigate } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type UserMe = {
  id: string;
  email: string;
  is_sysadmin: boolean;
};

export type DashboardProject = {
  id: string;
  name: string;
  created_at: string;
};

type DashboardWorkspaceContextValue = {
  canCreateProject: boolean;
  createProject: (name: string) => Promise<DashboardProject>;
  inProjectArea: boolean;
  projectCount: number;
  projectLoadError: string | null;
  projectSectionLabel: string;
  projects: DashboardProject[];
  projectsReady: boolean;
  refreshProjects: () => Promise<void>;
  selectedProject: string;
  selectedProjectName: string;
  switchProject: (projectId: string) => void;
};

const DashboardWorkspaceContext = createContext<DashboardWorkspaceContextValue | null>(null);

export function DashboardWorkspaceProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [canCreateProject, setCanCreateProject] = useState(false);
  const [projectLoadError, setProjectLoadError] = useState<string | null>(null);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [projectsReady, setProjectsReady] = useState(false);
  const [dashboardProjectId, setDashboardProjectId] = useState("");

  const inventoryMatch = matchPath("/projects/:projectId/inventory", location.pathname);
  const importMatch = matchPath("/projects/:projectId/import", location.pathname);
  const runMatch = matchPath("/projects/:projectId/runs/:runId", location.pathname);
  const overviewMatch = matchPath("/projects/:projectId/overview", location.pathname);
  const findingsMatch = matchPath("/projects/:projectId/findings", location.pathname);
  const changesMatch = matchPath("/projects/:projectId/changes", location.pathname);
  const sourcesMatch = matchPath("/projects/:projectId/sources", location.pathname);
  const comparisonMatch = matchPath("/projects/:projectId/comparisons/:comparisonId", location.pathname);
  const routeProjectId = inventoryMatch?.params.projectId
    || importMatch?.params.projectId
    || runMatch?.params.projectId
    || overviewMatch?.params.projectId
    || findingsMatch?.params.projectId
    || changesMatch?.params.projectId
    || sourcesMatch?.params.projectId
    || comparisonMatch?.params.projectId
    || "";
  const inProjectArea = location.pathname.startsWith("/projects");
  const projectSectionLabel = inventoryMatch
    ? "Inventory"
    : findingsMatch
      ? "Findings"
      : changesMatch || comparisonMatch
        ? "Changes"
        : sourcesMatch
          ? "Sources"
    : importMatch
      ? "Import Scan"
      : runMatch
        ? "Run Explorer"
        : "Dashboard";
  const selectedProject = routeProjectId || dashboardProjectId;

  useEffect(() => {
    if (routeProjectId) {
      setDashboardProjectId(routeProjectId);
    }
  }, [routeProjectId]);

  async function refreshProjects() {
    const data = ((await apiFetch("/projects")) as DashboardProject[]) || [];
    setProjects(data);
    setDashboardProjectId((current) => {
      if (routeProjectId && data.some((project) => project.id === routeProjectId)) {
        return routeProjectId;
      }
      if (current && data.some((project) => project.id === current)) {
        return current;
      }
      return data[0]?.id || "";
    });
  }

  async function createProject(name: string) {
    const created = (await apiFetch("/projects", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    })) as DashboardProject;
    setDashboardProjectId(created.id);
    await refreshProjects();
    return created;
  }

  function switchProject(projectId: string) {
    setDashboardProjectId(projectId);
    if (!inProjectArea) return;

    if (inventoryMatch) {
      const portableParams = new URLSearchParams(location.search);
      portableParams.delete("runs");
      const portableQuery = portableParams.toString();
      navigate(`/projects/${projectId}/inventory${portableQuery ? `?${portableQuery}` : ""}`);
      return;
    }
    if (findingsMatch) {
      navigate(`/projects/${projectId}/findings`);
      return;
    }
    if (changesMatch || comparisonMatch) {
      navigate(`/projects/${projectId}/changes`);
      return;
    }
    if (sourcesMatch) {
      navigate(`/projects/${projectId}/sources`);
      return;
    }
    if (overviewMatch) {
      navigate(`/projects/${projectId}/overview`);
      return;
    }
    if (importMatch) {
      navigate(`/projects/${projectId}/import`);
      return;
    }
    if (runMatch) {
      navigate(`/projects/${projectId}/inventory`);
      return;
    }
    navigate("/projects");
  }

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function loadWorkspace() {
      setProjectsReady(false);
      try {
        const [meData, projectData] = await Promise.all([
          apiFetch("/auth/me", { signal: controller.signal }),
          apiFetch("/projects", { signal: controller.signal }),
        ]);
        if (cancelled) return;
        setCanCreateProject(!!(meData as UserMe | null)?.is_sysadmin);
        const rows = ((projectData as DashboardProject[]) || []) as DashboardProject[];
        setProjects(rows);
        setDashboardProjectId((current) => {
          if (routeProjectId && rows.some((project) => project.id === routeProjectId)) {
            return routeProjectId;
          }
          if (current && rows.some((project) => project.id === current)) {
            return current;
          }
          return rows[0]?.id || "";
        });
        setProjectLoadError(null);
      } catch (err) {
        if (cancelled) return;
        setCanCreateProject(false);
        setProjects([]);
        setDashboardProjectId("");
        setProjectLoadError(err instanceof Error ? err.message : "Failed to load dashboard workspace");
      } finally {
        if (!cancelled) {
          setProjectsReady(true);
        }
      }
    }

    loadWorkspace().catch(() => undefined);
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [routeProjectId]);

  const value = {
    canCreateProject,
    createProject,
    inProjectArea,
    projectCount: projects.length,
    projectLoadError,
    projectSectionLabel,
    projects,
    projectsReady,
    refreshProjects,
    selectedProject,
    selectedProjectName: projects.find((project) => project.id === selectedProject)?.name || "",
    switchProject,
  };

  return <DashboardWorkspaceContext.Provider value={value}>{children}</DashboardWorkspaceContext.Provider>;
}

export function useDashboardWorkspace() {
  const context = useContext(DashboardWorkspaceContext);
  if (!context) {
    throw new Error("useDashboardWorkspace must be used within DashboardWorkspaceProvider");
  }
  return context;
}
