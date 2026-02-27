import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { apiFetch } from "@/lib/api";

type Project = { id: string; name: string; created_at: string };
type Run = {
  id: string;
  name: string;
  status: string;
  created_at: string;
  summary: { endpoints?: number; resources?: number; items?: number; errors?: number };
};

export function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/projects")
      .then((data) => {
        const projectRows = (data || []) as Project[];
        setProjects(projectRows);
        if (projectRows.length > 0) {
          setSelectedProject(projectRows[0].id);
        }
      })
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    setCursor(null);
    setCursorHistory([]);
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    const query = new URLSearchParams({ limit: "50" });
    if (cursor) {
      query.set("cursor", cursor);
    }
    apiFetch(`/projects/${selectedProject}/runs?${query.toString()}`)
      .then((data) => {
        setRuns((data?.items || []) as Run[]);
        setNextCursor((data?.next_cursor as string | null) || null);
      })
      .catch((err) => setError(err.message));
  }, [selectedProject, cursor]);

  const selectedProjectName = useMemo(
    () => projects.find((project) => project.id === selectedProject)?.name || "",
    [projects, selectedProject],
  );

  function moveNext() {
    if (!nextCursor) return;
    setCursorHistory((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }

  function movePrev() {
    setCursorHistory((prev) => {
      if (prev.length === 0) return prev;
      const copy = [...prev];
      const previousCursor = copy.pop() ?? null;
      setCursor(previousCursor);
      return copy;
    });
  }

  return (
    <section className="space-y-6">
      <div className="panel flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">Select a project and browse scan runs.</p>
        </div>
        <select
          className="rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
          value={selectedProject}
          onChange={(event) => setSelectedProject(event.target.value)}
        >
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </div>

      {error ? <p className="text-sm text-red-600">{error}</p> : null}

      <div className="panel overflow-x-auto">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Runs in {selectedProjectName || "selected project"}</h2>
          <div className="flex items-center gap-2">
            <button
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={movePrev}
              disabled={cursorHistory.length === 0}
            >
              Prev
            </button>
            <button
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={moveNext}
              disabled={!nextCursor}
            >
              Next
            </button>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Created</th>
              <th>Counts</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>{run.name}</td>
                <td>{run.status}</td>
                <td>{new Date(run.created_at).toLocaleString()}</td>
                <td>
                  e:{run.summary?.endpoints || 0} r:{run.summary?.resources || 0} i:{run.summary?.items || 0}
                </td>
                <td>
                  <Link
                    className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-semibold uppercase hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    to={`/projects/${selectedProject}/runs/${run.id}`}
                  >
                    Open
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
