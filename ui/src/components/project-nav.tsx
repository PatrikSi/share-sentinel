import { Link, useLocation } from "react-router-dom";

import { useDashboardWorkspace } from "@/lib/dashboard-workspace";

const PROJECT_NAV_ITEMS = [
  { key: "overview", label: "Overview" },
  { key: "findings", label: "Findings" },
  { key: "inventory", label: "Inventory" },
  { key: "changes", label: "Changes" },
  { key: "sources", label: "Sources" },
] as const;

function activeSection(pathname: string): (typeof PROJECT_NAV_ITEMS)[number]["key"] {
  if (pathname.includes("/findings")) return "findings";
  if (pathname.includes("/inventory")) return "inventory";
  if (pathname.includes("/changes") || pathname.includes("/comparisons/")) return "changes";
  if (pathname.includes("/sources")) return "sources";
  return "overview";
}

export function ProjectNav() {
  const location = useLocation();
  const { inProjectArea, selectedProject, selectedProjectName } = useDashboardWorkspace();

  if (!inProjectArea || !selectedProject) return null;

  const current = activeSection(location.pathname);
  return (
    <div className="project-nav-shell">
      <nav aria-label={`${selectedProjectName || "Project"} workspace`} className="project-nav">
        {PROJECT_NAV_ITEMS.map((item) => (
          <Link
            aria-current={current === item.key ? "page" : undefined}
            className={`project-nav-link ${current === item.key ? "is-active" : ""}`}
            key={item.key}
            to={`/projects/${encodeURIComponent(selectedProject)}/${item.key}`}
          >
            {item.label}
          </Link>
        ))}
        <Link
          aria-label="Collection data is snapshot evidence; open Sources to verify freshness"
          className="project-nav-freshness"
          title="Collection data is not live. Verify source freshness before drawing conclusions."
          to={`/projects/${encodeURIComponent(selectedProject)}/sources`}
        >
          <span aria-hidden="true" /> Snapshot evidence
        </Link>
      </nav>
    </div>
  );
}
