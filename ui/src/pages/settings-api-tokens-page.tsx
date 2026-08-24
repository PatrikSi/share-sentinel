import { FormEvent, useEffect, useMemo, useState } from "react";

import { Dialog } from "@/components/dialog";
import { SecretReveal } from "@/components/secret-reveal";
import { StatePanel } from "@/components/state-panel";
import { apiFetch } from "@/lib/api";

type ApiTokenRow = {
  id: string;
  user_id: string;
  user_email: string;
  project_id: string;
  project_name: string;
  name: string;
  role: string;
  scopes: string[];
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
};

type UserOption = { id: string; email: string };
type ProjectOption = { id: string; name: string };
type ScopeCatalog = {
  allowed_scopes: string[];
  defaults_by_role: Record<string, string[]>;
};

type TokenCreateResponse = {
  token: string;
  token_meta: ApiTokenRow;
};

type PendingTokenAction =
  | { kind: "rotate"; token: ApiTokenRow }
  | { kind: "revoke"; token: ApiTokenRow }
  | null;

const ROLES = ["viewer", "operator", "admin"];

function formatTime(value: string | null): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "N/A";
  return parsed.toLocaleString();
}

function parseScopesCsv(raw: string): string[] {
  return raw
    .split(",")
    .map((scope) => scope.trim().toLowerCase())
    .filter(Boolean);
}

function tokenStatus(token: ApiTokenRow): { label: string; className: string } {
  if (token.revoked_at) return { label: "Revoked", className: "settings-badge settings-badge-danger" };
  if (!token.expires_at) return { label: "Never expires", className: "settings-badge settings-badge-warning" };
  const expiresAt = new Date(token.expires_at);
  if (!Number.isNaN(expiresAt.getTime()) && expiresAt.getTime() < Date.now()) {
    return { label: "Expired", className: "settings-badge settings-badge-warning" };
  }
  return { label: "Active", className: "settings-badge settings-badge-positive" };
}

export function SettingsApiTokensPage() {
  const [tokens, setTokens] = useState<ApiTokenRow[]>([]);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [scopeCatalog, setScopeCatalog] = useState<ScopeCatalog | null>(null);
  const [ownerOptions, setOwnerOptions] = useState<UserOption[]>([]);
  const [ownerQuery, setOwnerQuery] = useState("");
  const [selectedOwnerId, setSelectedOwnerId] = useState("");
  const [ownerLoading, setOwnerLoading] = useState(false);

  const [loading, setLoading] = useState(false);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [projectFilterDraft, setProjectFilterDraft] = useState("all");
  const [projectFilter, setProjectFilter] = useState("all");

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [createProjectId, setCreateProjectId] = useState("");
  const [createName, setCreateName] = useState("collector-token");
  const [createRole, setCreateRole] = useState("operator");
  const [createUseDefaults, setCreateUseDefaults] = useState(true);
  const [createScopesCsv, setCreateScopesCsv] = useState("");
  const [createExpiryDays, setCreateExpiryDays] = useState("90");

  const [editTokenId, setEditTokenId] = useState("");
  const [editName, setEditName] = useState("");
  const [editRole, setEditRole] = useState("operator");
  const [editUseDefaults, setEditUseDefaults] = useState(true);
  const [editScopesCsv, setEditScopesCsv] = useState("");
  const [editExpiryDays, setEditExpiryDays] = useState("");

  const [secretReveal, setSecretReveal] = useState<{ label: string; secret: string } | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingTokenAction>(null);
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [pendingActionSubmitting, setPendingActionSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const editToken = useMemo(() => tokens.find((token) => token.id === editTokenId) || null, [tokens, editTokenId]);
  const tokenStats = useMemo(() => {
    const total = tokens.length;
    const active = tokens.filter((token) => !token.revoked_at).length;
    const revoked = total - active;
    const neverExpires = tokens.filter((token) => !token.revoked_at && !token.expires_at).length;
    return { total, active, revoked, neverExpires };
  }, [tokens]);

  async function loadReferenceData() {
    const [projectsData, scopeData] = await Promise.all([apiFetch("/settings/projects"), apiFetch("/settings/api-token-scopes")]);
    setProjects((projectsData || []) as ProjectOption[]);
    setScopeCatalog(scopeData as ScopeCatalog);
  }

  async function loadOwnerOptions(queryText: string) {
    setOwnerLoading(true);
    try {
      const query = new URLSearchParams({ limit: "30", is_active: "true", is_approved: "true" });
      if (queryText.trim()) query.set("search", queryText.trim());
      const data = await apiFetch(`/users?${query.toString()}`);
      setOwnerOptions((((data?.items as UserOption[]) || []) as UserOption[]).sort((a, b) => a.email.localeCompare(b.email)));
    } finally {
      setOwnerLoading(false);
    }
  }

  async function loadTokens() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
      if (projectFilter !== "all") query.set("project_id", projectFilter);
      if (cursor) query.set("cursor", cursor);
      const data = await apiFetch(`/settings/api-tokens?${query.toString()}`);
      setTokens((data?.items || []) as ApiTokenRow[]);
      setNextCursor((data?.next_cursor as string | null) || null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadReferenceData().catch((err) => setError(err instanceof Error ? err.message : "Failed to load token metadata"));
  }, []);

  useEffect(() => {
    loadTokens().catch((err) => setError(err instanceof Error ? err.message : "Failed to load token inventory"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, projectFilter, search]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadOwnerOptions(ownerQuery).catch((err) => setError(err instanceof Error ? err.message : "Failed to search users"));
    }, 200);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownerQuery]);

  useEffect(() => {
    if (!editToken) return;
    setEditName(editToken.name);
    setEditRole(editToken.role);
    setEditScopesCsv(editToken.scopes.join(", "));
    setEditUseDefaults(false);
    setEditExpiryDays("");
  }, [editToken]);

  async function createToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (createSubmitting) return;
    if (!selectedOwnerId || !createProjectId) {
      setError("Select both an owner and a project before issuing a token.");
      return;
    }
    setError(null);
    setInfo(null);
    setSecretReveal(null);
    setCreateSubmitting(true);

    const expiryDays = createExpiryDays.trim() ? Number.parseInt(createExpiryDays.trim(), 10) : Number.NaN;
    const scopes = createUseDefaults ? [] : parseScopesCsv(createScopesCsv);

    try {
      const data = (await apiFetch("/settings/api-tokens", {
        method: "POST",
        body: JSON.stringify({
          user_id: selectedOwnerId,
          project_id: createProjectId,
          name: createName,
          role: createRole,
          expires_in_days: Number.isFinite(expiryDays) && expiryDays > 0 ? expiryDays : undefined,
          scopes,
        }),
      })) as TokenCreateResponse;
      setSecretReveal({ label: `Token secret for ${data.token_meta.name}`, secret: data.token });
      setInfo("API token issued.");
      setShowCreateForm(false);
      setTokens((prev) => [data.token_meta, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create token");
    } finally {
      setCreateSubmitting(false);
    }
  }

  async function saveTokenUpdates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (editSubmitting || !editToken) return;
    setError(null);
    setInfo(null);
    setEditSubmitting(true);

    const expiryDays = editExpiryDays.trim() ? Number.parseInt(editExpiryDays.trim(), 10) : Number.NaN;
    const scopes = editUseDefaults ? [] : parseScopesCsv(editScopesCsv);

    try {
      const data = (await apiFetch(`/settings/api-tokens/${editToken.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: editName,
          role: editRole,
          scopes,
          expires_in_days: Number.isFinite(expiryDays) && expiryDays > 0 ? expiryDays : undefined,
        }),
      })) as ApiTokenRow;
      setInfo("Token updated.");
      setTokens((prev) => prev.map((token) => (token.id === data.id ? data : token)));
      setEditTokenId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update token");
    } finally {
      setEditSubmitting(false);
    }
  }

  async function confirmPendingAction() {
    if (!pendingAction || pendingActionSubmitting) return;
    setError(null);
    setInfo(null);
    setPendingActionSubmitting(true);

    try {
      if (pendingAction.kind === "rotate") {
        const data = (await apiFetch(`/settings/api-tokens/${pendingAction.token.id}/rotate`, { method: "POST" })) as TokenCreateResponse;
        setSecretReveal({ label: `Rotated secret for ${data.token_meta.name}`, secret: data.token });
        setInfo("Token rotated.");
        setTokens((prev) => prev.map((token) => (token.id === data.token_meta.id ? data.token_meta : token)));
      } else {
        await apiFetch(`/settings/api-tokens/${pendingAction.token.id}`, { method: "DELETE" });
        setInfo("Token revoked.");
        setTokens((prev) =>
          prev.map((token) => (token.id === pendingAction.token.id ? { ...token, revoked_at: new Date().toISOString() } : token)),
        );
      }
      setPendingAction(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${pendingAction.kind} token`);
    } finally {
      setPendingActionSubmitting(false);
    }
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursor(null);
    setHistory([]);
    setSearch(searchDraft.trim());
    setProjectFilter(projectFilterDraft);
  }

  function clearFilters() {
    setCursor(null);
    setHistory([]);
    setSearchDraft("");
    setSearch("");
    setProjectFilterDraft("all");
    setProjectFilter("all");
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
          <h2 className="settings-page-title">API Tokens</h2>
          <p className="settings-page-copy">Review token inventory first, then issue or modify machine credentials with explicit ownership.</p>
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
            {showCreateForm ? "Close Token Form" : "Issue Token"}
          </button>
          <button className="settings-button" onClick={() => loadTokens().catch(() => undefined)} type="button">
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
      {secretReveal ? (
        <div className="settings-panel">
          <SecretReveal label={secretReveal.label} secret={secretReveal.secret} onDismiss={() => setSecretReveal(null)} />
        </div>
      ) : null}

      <section className="settings-panel">
        <div className="settings-grid-3">
          <div className="settings-kpi">
            <span className="settings-kpi-label">Visible tokens</span>
            <span className="settings-kpi-value">{tokenStats.total}</span>
            <p className="settings-kpi-copy">Current page inventory.</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Active</span>
            <span className="settings-kpi-value">{tokenStats.active}</span>
            <p className="settings-kpi-copy">Not revoked on this page.</p>
          </div>
          <div className="settings-kpi">
            <span className="settings-kpi-label">Never expires</span>
            <span className="settings-kpi-value">{tokenStats.neverExpires}</span>
            <p className="settings-kpi-copy">Review long-lived credentials closely.</p>
          </div>
        </div>
      </section>

      {showCreateForm ? (
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Issue Token</h3>
              <p className="settings-panel-copy">Select the owner and project explicitly. Nothing is preselected.</p>
            </div>
          </div>

          <form className="mt-4 grid gap-4" onSubmit={createToken}>
            <div className="settings-grid-2">
              <div className="grid gap-4">
                <label className="settings-field">
                  <span className="settings-label">Owner search</span>
                  <input
                    className="settings-input"
                    placeholder="Search active approved users"
                    value={ownerQuery}
                    onChange={(event) => setOwnerQuery(event.target.value)}
                  />
                </label>

                <label className="settings-field">
                  <span className="settings-label">Owner</span>
                  <select className="settings-select" value={selectedOwnerId} onChange={(event) => setSelectedOwnerId(event.target.value)}>
                    <option value="">{ownerLoading ? "Searching users..." : "Select owner"}</option>
                    {ownerOptions.map((user) => (
                      <option key={user.id} value={user.id}>
                        {user.email}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="settings-field">
                  <span className="settings-label">Project</span>
                  <select className="settings-select" value={createProjectId} onChange={(event) => setCreateProjectId(event.target.value)}>
                    <option value="">Select project</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="grid gap-4">
                <label className="settings-field">
                  <span className="settings-label">Token name</span>
                  <input className="settings-input" value={createName} onChange={(event) => setCreateName(event.target.value)} required />
                </label>

                <label className="settings-field">
                  <span className="settings-label">Role</span>
                  <select className="settings-select max-w-[220px]" value={createRole} onChange={(event) => setCreateRole(event.target.value)}>
                    {ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="settings-field">
                  <span className="settings-label">Expiry days</span>
                  <input
                    className="settings-input max-w-[220px]"
                    inputMode="numeric"
                    value={createExpiryDays}
                    onChange={(event) => setCreateExpiryDays(event.target.value)}
                  />
                </label>
              </div>
            </div>

            <div className="grid gap-3">
              <label className="inline-flex items-center gap-2 text-sm">
                <input checked={createUseDefaults} onChange={(event) => setCreateUseDefaults(event.target.checked)} type="checkbox" />
                Use default scopes for the selected role
              </label>

              {!createUseDefaults ? (
                <label className="settings-field">
                  <span className="settings-label">Scopes</span>
                  <input
                    className="settings-input"
                    placeholder="read:projects, write:runs"
                    value={createScopesCsv}
                    onChange={(event) => setCreateScopesCsv(event.target.value)}
                  />
                </label>
              ) : null}

              {scopeCatalog ? (
                <p className="settings-meta">Allowed scopes: {scopeCatalog.allowed_scopes.join(", ")}</p>
              ) : null}
            </div>

            <div className="settings-toolbar">
              <button className="settings-button-primary" disabled={createSubmitting} type="submit">
                {createSubmitting ? "Issuing..." : "Issue Token"}
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
            <h3 className="settings-panel-title">Inventory</h3>
            <p className="settings-panel-copy">Search and filter the current global token inventory before making changes.</p>
          </div>
        </div>

        <form className="mt-4 grid gap-4" onSubmit={applyFilters}>
          <div className="settings-grid-2">
            <label className="settings-field">
              <span className="settings-label">Search</span>
              <input
                className="settings-input"
                placeholder="Token name, owner, project, or token ID"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
              />
            </label>

            <label className="settings-field">
              <span className="settings-label">Project</span>
              <select className="settings-select" value={projectFilterDraft} onChange={(event) => setProjectFilterDraft(event.target.value)}>
                <option value="all">All projects</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="settings-toolbar">
            <button className="settings-button-primary" type="submit">
              Apply Filters
            </button>
            <button className="settings-button" onClick={clearFilters} type="button">
              Clear
            </button>
          </div>
        </form>

        {loading ? (
          <div className="mt-4">
            <StatePanel title="Loading Tokens" description="Fetching token inventory." />
          </div>
        ) : tokens.length === 0 ? (
          <div className="mt-4 settings-empty">No tokens matched the current filters.</div>
        ) : (
          <div className="mt-4 settings-table-wrap">
            <table className="settings-table">
              <caption className="sr-only">API tokens matching the current filters</caption>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Owner</th>
                  <th>Project</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Last used</th>
                  <th>Expires</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((token) => {
                  const status = tokenStatus(token);
                  return (
                    <tr key={token.id}>
                      <td>
                        <div className="font-semibold">{token.name}</div>
                        <div className="settings-meta">{token.id}</div>
                      </td>
                      <td>{token.user_email}</td>
                      <td>{token.project_name}</td>
                      <td>{token.role}</td>
                      <td>
                        <span className={status.className}>{status.label}</span>
                      </td>
                      <td>{formatTime(token.last_used_at)}</td>
                      <td>{formatTime(token.expires_at)}</td>
                      <td className="text-right">
                        <div className="settings-toolbar justify-end">
                          {!token.revoked_at ? (
                            <button className="settings-button" onClick={() => setEditTokenId(token.id)} type="button">
                              Edit
                            </button>
                          ) : null}
                          {!token.revoked_at ? (
                            <button className="settings-button" onClick={() => setPendingAction({ kind: "rotate", token })} type="button">
                              Rotate
                            </button>
                          ) : null}
                          {!token.revoked_at ? (
                            <button className="settings-button-danger" onClick={() => setPendingAction({ kind: "revoke", token })} type="button">
                              Revoke
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
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

      {editToken ? (
        <section className="settings-panel">
          <div className="settings-panel-header">
            <div>
              <h3 className="settings-panel-title">Edit Token</h3>
              <p className="settings-panel-copy">{editToken.name} for {editToken.user_email} on {editToken.project_name}</p>
            </div>
          </div>

          <form className="mt-4 grid gap-4" onSubmit={saveTokenUpdates}>
            <div className="settings-grid-2">
              <label className="settings-field">
                <span className="settings-label">Name</span>
                <input className="settings-input" value={editName} onChange={(event) => setEditName(event.target.value)} required />
              </label>

              <label className="settings-field">
                <span className="settings-label">Role</span>
                <select className="settings-select max-w-[220px]" value={editRole} onChange={(event) => setEditRole(event.target.value)}>
                  {ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="settings-grid-2">
              <label className="settings-field">
                <span className="settings-label">Expiry days</span>
                <input
                  className="settings-input max-w-[220px]"
                  inputMode="numeric"
                  value={editExpiryDays}
                  onChange={(event) => setEditExpiryDays(event.target.value)}
                />
              </label>
            </div>

            <label className="inline-flex items-center gap-2 text-sm">
              <input checked={editUseDefaults} onChange={(event) => setEditUseDefaults(event.target.checked)} type="checkbox" />
              Reset scopes to role defaults
            </label>

            {!editUseDefaults ? (
              <label className="settings-field">
                <span className="settings-label">Scopes</span>
                <input className="settings-input" value={editScopesCsv} onChange={(event) => setEditScopesCsv(event.target.value)} />
              </label>
            ) : null}

            <div className="settings-toolbar">
              <button className="settings-button-primary" disabled={editSubmitting} type="submit">
                {editSubmitting ? "Saving..." : "Save Changes"}
              </button>
              <button className="settings-button" onClick={() => setEditTokenId("")} type="button">
                Cancel
              </button>
            </div>
          </form>
        </section>
      ) : null}

      <Dialog
        open={pendingAction !== null}
        onClose={() => setPendingAction(null)}
        title={pendingAction?.kind === "rotate" ? `Rotate ${pendingAction.token.name}?` : `Revoke ${pendingAction?.token.name || "token"}?`}
        description={
          pendingAction?.kind === "rotate"
            ? "This will replace the current secret and show the new value once."
            : "This permanently revokes the selected token."
        }
        footer={
          <>
            <button className="settings-button" onClick={() => setPendingAction(null)} type="button">
              Cancel
            </button>
            <button
              className={pendingAction?.kind === "revoke" ? "settings-button-danger" : "settings-button-primary"}
              disabled={pendingActionSubmitting}
              onClick={() => confirmPendingAction().catch(() => undefined)}
              type="button"
            >
              {pendingAction?.kind === "rotate" ? "Rotate Token" : "Revoke Token"}
            </button>
          </>
        }
      >
        {pendingAction ? (
          <div className="settings-note-list">
            <p>Token: {pendingAction.token.name}</p>
            <p>Owner: {pendingAction.token.user_email}</p>
            <p>Project: {pendingAction.token.project_name}</p>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
