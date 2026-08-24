import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { StatePanel } from "@/components/state-panel";
import { apiFetch } from "@/lib/api";

type ProjectCatalogRow = {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
  admin_count: number;
  token_count: number;
  active_token_count: number;
  run_count: number;
  artifact_count: number;
  blocking_run_count: number;
  has_blocking_runs: boolean;
  last_run_at: string | null;
};

function formatDateTime(value: string | null): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

export function SettingsProjectsPage() {
  const [projects, setProjects] = useState<ProjectCatalogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [creating, setCreating] = useState(false);

  async function loadProjects() {
    setLoading(true);
    setError(null);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/projects/catalog?${query.toString()}`);
      setProjects((data?.items || []) as ProjectCatalogRow[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load projects");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadProjects().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) return;
    setCreating(true);
    setError(null);
    setInfo(null);
    try {
      const data = await apiFetch("/projects", {
        method: "POST",
        body: JSON.stringify({ name: newProjectName.trim() }),
      });
      setInfo(`Project created: ${(data as { name?: string }).name || newProjectName.trim()}`);
      setNewProjectName("");
      setShowCreateForm(false);
      setCursor(null);
      setHistory([]);
      await loadProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create project");
    } finally {
      setCreating(false);
    }
  }

  function applySearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursor(null);
    setHistory([]);
    setSearch(searchDraft.trim());
  }

  function clearSearch() {
    setCursor(null);
    setHistory([]);
    setSearchDraft("");
    setSearch("");
  }

  function previousPage() {
    if (history.length === 0) return;
    const copy = [...history];
    const previous = copy.pop() ?? null;
    setHistory(copy);
    setCursor(previous);
  }

  function nextPage() {
    if (!nextCursor) return;
    setHistory((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }

  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <h2 className="settings-page-title">Projects</h2>
          <p className="settings-page-copy">Manage project ownership, current footprint, and cleanup from one catalog.</p>
        </div>
        <div className="settings-toolbar">
          <button
            className="settings-button"
            onClick={() => {
              setError(null);
              setInfo(null);
              setShowCreateForm((open) => !open);
            }}
            type="button"
          >
            {showCreateForm ? "Close Project Form" : "New Project"}
          </button>
          <button className="settings-button" onClick={() => loadProjects().catch(() => undefined)} type="button">
            Refresh
          </button>
        </div>
      </div>

      {error ? (
        <div className="settings-panel">
          <p className="text-sm text-rose-700 dark:text-rose-200">{error}</p>
        </div>
      ) : null}
      {info ? (
        <div className="settings-panel">
          <p className="text-sm text-emerald-700 dark:text-emerald-200">{info}</p>
        </div>
      ) : null}

      {showCreateForm ? (
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Create Project</h3>
              <p className="settings-panel-copy">This creates the project and automatically grants the current sysadmin project-admin access.</p>
            </div>
          </div>

          <form className="mt-4 settings-toolbar" onSubmit={createProject}>
            <label className="settings-field min-w-[320px] flex-1">
              <span className="settings-label">Project name</span>
              <input
                className="settings-input"
                placeholder="Client East - Q2 Shares"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                required
              />
            </label>
            <div className="settings-toolbar">
              <button className="settings-button-primary" disabled={creating} type="submit">
                {creating ? "Creating..." : "Create Project"}
              </button>
              <button className="settings-button" onClick={() => setShowCreateForm(false)} type="button">
                Cancel
              </button>
            </div>
          </form>
        </section>
      ) : null}

      <section className="settings-panel">
        <div className="settings-panel-header">
          <div>
            <h3 className="settings-panel-title">Catalog</h3>
            <p className="settings-panel-copy">Compare members, tokens, run volume, and delete blockers before opening a project.</p>
          </div>
        </div>

        <form className="mt-4 settings-toolbar" onSubmit={applySearch}>
          <label className="settings-field min-w-[320px] flex-1">
            <span className="settings-label">Search</span>
            <input
              className="settings-input"
              type="search"
              placeholder="Project name or project ID"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
            />
          </label>
          <div className="settings-toolbar">
            <button className="settings-button-primary" type="submit">
              Apply Search
            </button>
            <button className="settings-button" onClick={clearSearch} type="button">
              Clear
            </button>
          </div>
        </form>

        {loading ? (
          <div className="mt-4">
            <StatePanel title="Loading Projects" description="Fetching the current project catalog." />
          </div>
        ) : projects.length === 0 ? (
          <div className="mt-4 settings-empty">No projects matched the current search.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <caption className="sr-only">Projects matching the current search</caption>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Members</th>
                  <th>Tokens</th>
                  <th>Runs</th>
                  <th>Last run</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => (
                  <tr key={project.id}>
                    <td>
                      <div className="font-semibold">{project.name}</div>
                      <div className="settings-meta">{project.id}</div>
                    </td>
                    <td>
                      <div>{project.member_count} total</div>
                      <div className="settings-meta">{project.admin_count} admins</div>
                    </td>
                    <td>
                      <div>{project.active_token_count} active</div>
                      <div className="settings-meta">{project.token_count} total</div>
                    </td>
                    <td>
                      <div>{project.run_count} runs</div>
                      <div className="settings-meta">{project.artifact_count} artifacts</div>
                    </td>
                    <td>{formatDateTime(project.last_run_at)}</td>
                    <td>
                      {project.has_blocking_runs ? (
                        <span className="settings-badge settings-badge-warning">{project.blocking_run_count} blocking</span>
                      ) : (
                        <span className="settings-badge settings-badge-neutral">Ready</span>
                      )}
                    </td>
                    <td className="text-right">
                      <Link className="settings-button" to={`/settings/projects/${project.id}`}>
                        Open
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4 settings-toolbar">
          <button className="settings-button" disabled={history.length === 0} onClick={previousPage} type="button">
            Previous
          </button>
          <button className="settings-button" disabled={!nextCursor} onClick={nextPage} type="button">
            Next
          </button>
        </div>
      </section>
    </div>
  );
}
