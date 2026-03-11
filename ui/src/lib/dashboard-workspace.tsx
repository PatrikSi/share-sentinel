import { createContext, ReactNode, useContext, useEffect, useState } from "react";

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
  projectLoadError: string | null;
  projects: DashboardProject[];
  projectsReady: boolean;
  refreshProjects: () => Promise<void>;
  selectedProject: string;
  selectedProjectName: string;
  setSelectedProject: (projectId: string) => void;
};

const DashboardWorkspaceContext = createContext<DashboardWorkspaceContextValue | null>(null);

export function DashboardWorkspaceProvider({ children }: { children: ReactNode }) {
  const [canCreateProject, setCanCreateProject] = useState(false);
  const [projectLoadError, setProjectLoadError] = useState<string | null>(null);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [projectsReady, setProjectsReady] = useState(false);
  const [selectedProject, setSelectedProject] = useState("");

  async function refreshProjects() {
    const data = ((await apiFetch("/projects")) as DashboardProject[]) || [];
    setProjects(data);
    setSelectedProject((current) => {
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
    setSelectedProject(created.id);
    await refreshProjects();
    return created;
  }

  useEffect(() => {
    let cancelled = false;

    async function loadWorkspace() {
      setProjectsReady(false);
      try {
        const [meData, projectData] = await Promise.all([apiFetch("/auth/me"), apiFetch("/projects")]);
        if (cancelled) return;
        setCanCreateProject(!!(meData as UserMe | null)?.is_sysadmin);
        const rows = ((projectData as DashboardProject[]) || []) as DashboardProject[];
        setProjects(rows);
        setSelectedProject((current) => {
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
        setSelectedProject("");
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
    };
  }, []);

  const value = {
    canCreateProject,
    createProject,
    projectLoadError,
    projects,
    projectsReady,
    refreshProjects,
    selectedProject,
    selectedProjectName: projects.find((project) => project.id === selectedProject)?.name || "",
    setSelectedProject,
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
