import { FormEvent, useEffect, useMemo, useState } from "react";

import { Dialog } from "@/components/dialog";
import { SecretReveal } from "@/components/secret-reveal";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
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
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "N/A";
  return date.toLocaleString();
}

function parseScopesCsv(raw: string): string[] {
  return raw
    .split(",")
    .map((scope) => scope.trim().toLowerCase())
    .filter(Boolean);
}

export function SettingsApiTokensPage() {
  const [tokens, setTokens] = useState<ApiTokenRow[]>([]);
  const [ownerOptions, setOwnerOptions] = useState<UserOption[]>([]);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [scopeCatalog, setScopeCatalog] = useState<ScopeCatalog | null>(null);
  const [selectedOwner, setSelectedOwner] = useState<UserOption | null>(null);
  const [ownerQuery, setOwnerQuery] = useState("");
  const [ownerLoading, setOwnerLoading] = useState(false);

  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

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
  const [editNeverExpires, setEditNeverExpires] = useState(false);

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
    const projectRows = (projectsData || []) as ProjectOption[];

    setProjects(projectRows);
    setScopeCatalog(scopeData as ScopeCatalog);
    if (!createProjectId && projectRows.length > 0) setCreateProjectId(projectRows[0].id);
  }

  async function loadOwnerOptions(queryText: string) {
    setOwnerLoading(true);
    try {
      const query = new URLSearchParams({ limit: "20", is_active: "true", is_approved: "true" });
      if (queryText.trim()) query.set("search", queryText.trim());
      const data = await apiFetch(`/users?${query.toString()}`);
      const rows = (((data?.items as UserOption[]) || []) as UserOption[]).sort((a, b) => a.email.localeCompare(b.email));
      setOwnerOptions(rows);
      if (!selectedOwner && rows.length > 0) {
        setSelectedOwner(rows[0]);
      }
    } finally {
      setOwnerLoading(false);
    }
  }

  async function loadTokens() {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (search.trim()) query.set("q", search.trim());
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadOwnerOptions(ownerQuery).catch((err) => setError(err instanceof Error ? err.message : "Failed to search token owners"));
    }, 200);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownerQuery]);

  useEffect(() => {
    loadTokens().catch((err) => setError(err instanceof Error ? err.message : "Failed to load API tokens"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, search]);

  useEffect(() => {
    if (!editToken) return;
    setEditName(editToken.name);
    setEditRole(editToken.role);
    setEditScopesCsv(editToken.scopes.join(", "));
    setEditUseDefaults(false);
    setEditExpiryDays("");
    setEditNeverExpires(false);
  }, [editToken]);

  async function createToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (createSubmitting) return;
    if (!selectedOwner || !createProjectId) {
      setError("Select an active approved owner before creating a token.");
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
          user_id: selectedOwner.id,
          project_id: createProjectId,
          name: createName,
          role: createRole,
          expires_in_days: Number.isFinite(expiryDays) && expiryDays > 0 ? expiryDays : undefined,
          scopes,
        }),
      })) as TokenCreateResponse;
      setSecretReveal({ label: `Token secret for ${data.token_meta.name}`, secret: data.token });
      setInfo("API token created.");
      setTokens((prev) => [data.token_meta, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create token");
    } finally {
      setCreateSubmitting(false);
    }
  }

  async function saveTokenUpdates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (editSubmitting) return;
    if (!editToken) return;
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
          never_expires: editNeverExpires,
        }),
      })) as ApiTokenRow;
      setInfo("API token updated.");
      setTokens((prev) => prev.map((token) => (token.id === data.id ? data : token)));
      setEditTokenId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update token");
    } finally {
      setEditSubmitting(false);
    }
  }

  async function confirmPendingAction() {
    if (pendingActionSubmitting) return;
    if (!pendingAction) return;
    setError(null);
    setInfo(null);
    setPendingActionSubmitting(true);

    try {
      if (pendingAction.kind === "rotate") {
        const data = (await apiFetch(`/settings/api-tokens/${pendingAction.token.id}/rotate`, { method: "POST" })) as TokenCreateResponse;
        setSecretReveal({ label: `Rotated secret for ${data.token_meta.name}`, secret: data.token });
        setInfo("API token rotated.");
        setTokens((prev) => prev.map((token) => (token.id === data.token_meta.id ? data.token_meta : token)));
      } else {
        await apiFetch(`/settings/api-tokens/${pendingAction.token.id}`, { method: "DELETE" });
        setInfo("API token revoked.");
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

  function onSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursor(null);
    setHistory([]);
    setSearch(searchDraft.trim());
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
    <>
      {error ? (
        <div className="workspace-section">
          <StatusBanner tone="error" title="Token Request Failed">
            <p>{error}</p>
          </StatusBanner>
        </div>
      ) : null}
      {info ? (
        <div className="workspace-section">
          <StatusBanner tone="success" title="Token Update">
            <p>{info}</p>
          </StatusBanner>
        </div>
      ) : null}
      {secretReveal ? (
        <div className="workspace-section">
          <SecretReveal label={secretReveal.label} secret={secretReveal.secret} onDismiss={() => setSecretReveal(null)} />
        </div>
      ) : null}

      <div className="workspace-section grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_360px]">
        <div className="workspace-card space-y-4">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Create Token</p>
            <h2 className="mt-2 text-xl font-semibold">Issue a machine credential</h2>
            <p className="mt-1 text-sm text-slate-500">Pick the owner, project, and role first. Scope defaults keep new tokens aligned with policy.</p>
          </div>

          <form className="grid gap-4 md:grid-cols-2" onSubmit={createToken}>
            <div className="md:col-span-2">
              <label className="block text-sm">
                Owner search
                <input
                  className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  placeholder="Search active approved users by email"
                  value={ownerQuery}
                  onChange={(event) => setOwnerQuery(event.target.value)}
                />
              </label>
              <div className="mt-3 rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Selected owner</p>
                <p className="mt-1 text-sm font-semibold">{selectedOwner?.email || "Choose an owner below"}</p>
                {ownerLoading ? <p className="mt-3 text-xs text-slate-500">Searching users…</p> : null}
                {!ownerLoading && ownerOptions.length === 0 ? (
                  <p className="mt-3 text-xs text-slate-500">No active approved users matched the current search.</p>
                ) : null}
                {ownerOptions.length > 0 ? (
                  <div className="mt-3 flex max-h-36 flex-wrap gap-2 overflow-auto">
                    {ownerOptions.map((user) => {
                      const selected = selectedOwner?.id === user.id;
                      return (
                        <button
                          className={`rounded-full border px-3 py-1 text-xs transition ${
                            selected
                              ? "border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-200"
                              : "border-slate-300 bg-white text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:hover:bg-slate-800"
                          }`}
                          disabled={createSubmitting}
                          key={user.id}
                          onClick={() => {
                            setSelectedOwner(user);
                            setOwnerQuery(user.email);
                          }}
                          type="button"
                        >
                          {user.email}
                        </button>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            </div>
            <label className="block text-sm">
              Project
              <select
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={createSubmitting}
                value={createProjectId}
                onChange={(event) => setCreateProjectId(event.target.value)}
              >
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Token name
              <input
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={createSubmitting}
                value={createName}
                onChange={(event) => setCreateName(event.target.value)}
                minLength={1}
                maxLength={120}
                required
              />
            </label>
            <label className="block text-sm">
              Role
              <select
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={createSubmitting}
                value={createRole}
                onChange={(event) => setCreateRole(event.target.value)}
              >
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              Expiry days
              <input
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={createSubmitting}
                value={createExpiryDays}
                onChange={(event) => setCreateExpiryDays(event.target.value)}
                placeholder="90"
              />
            </label>
            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/80">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Default scopes</p>
              <p className="mt-1 text-sm font-semibold">{createRole}</p>
              <p className="mt-1 text-xs text-slate-500">{scopeCatalog?.defaults_by_role?.[createRole]?.join(", ") || "Loading defaults"}</p>
            </div>
            <label className="flex items-center gap-2 text-sm md:col-span-2">
              <input checked={createUseDefaults} disabled={createSubmitting} type="checkbox" onChange={(event) => setCreateUseDefaults(event.target.checked)} />
              Use the role defaults instead of custom scopes
            </label>
            {!createUseDefaults ? (
              <label className="block text-sm md:col-span-2">
                Custom scopes
                <input
                  className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  disabled={createSubmitting}
                  value={createScopesCsv}
                  onChange={(event) => setCreateScopesCsv(event.target.value)}
                  placeholder="read:projects, read:runs"
                />
              </label>
            ) : null}
            <div className="md:col-span-2">
              <button
                className="rounded-2xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                disabled={createSubmitting}
                type="submit"
              >
                {createSubmitting ? "Creating token..." : "Create token"}
              </button>
            </div>
          </form>
        </div>

        <div className="space-y-4">
          <section className="workspace-card">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Token Posture</p>
            <div className="mt-4 grid gap-3">
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Active</p>
                <p className="mt-1 text-2xl font-semibold">{tokenStats.active}</p>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Revoked</p>
                <p className="mt-1 text-2xl font-semibold">{tokenStats.revoked}</p>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Never expire</p>
                <p className="mt-1 text-2xl font-semibold">{tokenStats.neverExpires}</p>
              </div>
            </div>
          </section>

          <section className="workspace-card">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Scope Catalog</p>
            <h2 className="mt-2 text-xl font-semibold">Allowed scopes</h2>
            <div className="mt-4 space-y-4 text-sm">
              {(scopeCatalog?.defaults_by_role ? Object.entries(scopeCatalog.defaults_by_role) : []).map(([role, scopes]) => (
                <div key={role}>
                  <p className="font-semibold">{role}</p>
                  <p className="mt-1 text-xs text-slate-500">{scopes.join(", ") || "none"}</p>
                </div>
              ))}
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">All supported scopes</p>
                <p className="mt-2 max-h-[180px] overflow-auto text-xs text-slate-500">{scopeCatalog?.allowed_scopes.join(", ") || "Loading scope catalog"}</p>
              </div>
            </div>
          </section>
        </div>
      </div>

      <div className="workspace-section space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Token Directory</p>
            <h2 className="mt-2 text-xl font-semibold">Global API tokens</h2>
            <p className="mt-1 text-sm text-slate-500">Each card keeps the owner, project, scope posture, and administrative actions together.</p>
          </div>
          <form className="flex flex-wrap items-center gap-2" onSubmit={onSearch}>
            <input
              className="rounded-2xl border border-slate-300 px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-900"
              placeholder="Search token, owner, project, or id"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
            />
            <button className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700" type="submit">
              Search
            </button>
            <button
              className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              type="button"
              onClick={() => {
                setSearchDraft("");
                setSearch("");
                setCursor(null);
                setHistory([]);
              }}
            >
              Clear
            </button>
          </form>
        </div>

        {loading && tokens.length === 0 ? (
          <StatePanel title="Loading Tokens" description="Fetching the current token inventory and policy metadata." />
        ) : null}

        {!loading && tokens.length === 0 ? (
          <StatePanel title="No Tokens Found" description="Try a broader search or create the first API token for this deployment." />
        ) : null}

        <div className="space-y-3">
          {tokens.map((token) => (
            <article className="rounded-3xl border border-slate-200 bg-white/90 p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950/60" key={token.id}>
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">{token.revoked_at ? "Revoked token" : "Active token"}</p>
                  <h3 className="mt-1 text-lg font-semibold">{token.name}</h3>
                  <p className="mt-1 text-xs text-slate-500">{token.id}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
                    disabled={pendingActionSubmitting}
                    onClick={() => setEditTokenId(token.id)}
                    type="button"
                  >
                    Edit
                  </button>
                  <button
                    className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700 disabled:opacity-50"
                    disabled={!!token.revoked_at || pendingActionSubmitting}
                    onClick={() => {
                      if (pendingActionSubmitting) return;
                      setPendingAction({ kind: "rotate", token });
                    }}
                    type="button"
                  >
                    Rotate
                  </button>
                  <button
                    className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700 disabled:opacity-50"
                    disabled={!!token.revoked_at || pendingActionSubmitting}
                    onClick={() => {
                      if (pendingActionSubmitting) return;
                      setPendingAction({ kind: "revoke", token });
                    }}
                    type="button"
                  >
                    Revoke
                  </button>
                </div>
              </div>

              <div className="mt-4 grid gap-3 lg:grid-cols-4">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Owner</p>
                  <p className="mt-1 text-sm font-semibold">{token.user_email}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Project</p>
                  <p className="mt-1 text-sm font-semibold">{token.project_name}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Role</p>
                  <p className="mt-1 text-sm font-semibold">{token.role}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Status</p>
                  <p className="mt-1 text-sm font-semibold">{token.revoked_at ? "Revoked" : "Active"}</p>
                </div>
              </div>

              <div className="mt-4 grid gap-3 lg:grid-cols-3">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Created</p>
                  <p className="mt-1 text-sm font-semibold">{formatTime(token.created_at)}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Last used</p>
                  <p className="mt-1 text-sm font-semibold">{formatTime(token.last_used_at)}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/80">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Expiry</p>
                  <p className="mt-1 text-sm font-semibold">{formatTime(token.expires_at)}</p>
                </div>
              </div>

              <div className="mt-4">
                <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Scopes</p>
                <div className="mt-2 flex flex-wrap gap-2 text-xs">
                  {token.scopes.length === 0 ? (
                    <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800">No scopes assigned</span>
                  ) : (
                    token.scopes.map((scope) => (
                      <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-800" key={scope}>
                        {scope}
                      </span>
                    ))
                  )}
                </div>
              </div>
            </article>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <button
            className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700 disabled:opacity-50"
            type="button"
            onClick={previousPage}
            disabled={history.length === 0}
          >
            Previous
          </button>
          <button
            className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700 disabled:opacity-50"
            type="button"
            onClick={nextPage}
            disabled={!nextCursor}
          >
            Next
          </button>
        </div>
      </div>

      <Dialog
        open={!!editToken}
        title="Edit API Token"
        description={editToken ? `Update ${editToken.name} without leaving the current token directory.` : undefined}
        onClose={() => setEditTokenId("")}
      >
        {editToken ? (
          <form className="grid gap-4 md:grid-cols-2" onSubmit={saveTokenUpdates}>
            <label className="block text-sm">
              Name
              <input
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={editSubmitting}
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                minLength={1}
                maxLength={120}
                required
              />
            </label>
            <label className="block text-sm">
              Role
              <select
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                disabled={editSubmitting}
                value={editRole}
                onChange={(event) => setEditRole(event.target.value)}
              >
                {ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-sm">
              New expiry days
              <input
                className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                value={editExpiryDays}
                onChange={(event) => setEditExpiryDays(event.target.value)}
                placeholder="180"
                disabled={editSubmitting || editNeverExpires}
              />
            </label>
            <label className="flex items-center gap-2 text-sm md:mt-8">
              <input checked={editNeverExpires} disabled={editSubmitting} type="checkbox" onChange={(event) => setEditNeverExpires(event.target.checked)} />
              Never expire
            </label>
            <label className="flex items-center gap-2 text-sm md:col-span-2">
              <input checked={editUseDefaults} disabled={editSubmitting} type="checkbox" onChange={(event) => setEditUseDefaults(event.target.checked)} />
              Reset scopes to the selected role defaults
            </label>
            {!editUseDefaults ? (
              <label className="block text-sm md:col-span-2">
                Scopes
                <input
                  className="mt-1 w-full rounded-2xl border border-slate-300 px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
                  disabled={editSubmitting}
                  value={editScopesCsv}
                  onChange={(event) => setEditScopesCsv(event.target.value)}
                />
              </label>
            ) : null}
            <div className="md:col-span-2 flex justify-end gap-3">
              <button
                className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
                disabled={editSubmitting}
                onClick={() => setEditTokenId("")}
                type="button"
              >
                Cancel
              </button>
              <button
                className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
                disabled={editSubmitting}
                type="submit"
              >
                {editSubmitting ? "Saving..." : "Save token changes"}
              </button>
            </div>
          </form>
        ) : null}
      </Dialog>

      <Dialog
        open={!!pendingAction}
        title={pendingAction?.kind === "rotate" ? "Rotate token secret" : "Revoke token"}
        description={
          pendingAction
            ? pendingAction.kind === "rotate"
              ? `Rotate ${pendingAction.token.name}. Existing clients will stop working until they switch to the new secret.`
              : `Revoke ${pendingAction.token.name}. This immediately disables the token across ${pendingAction.token.project_name}.`
            : undefined
        }
        onClose={() => setPendingAction(null)}
        footer={
          <>
            <button
              className="rounded-2xl border border-slate-300 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] dark:border-slate-700"
              disabled={pendingActionSubmitting}
              onClick={() => setPendingAction(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="rounded-2xl bg-slate-900 px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
              disabled={pendingActionSubmitting}
              onClick={confirmPendingAction}
              type="button"
            >
              {pendingActionSubmitting ? "Working..." : pendingAction?.kind === "rotate" ? "Rotate secret" : "Revoke token"}
            </button>
          </>
        }
      >
        {pendingAction ? (
          <div className="space-y-3 text-sm text-slate-600 dark:text-slate-300">
            <p>
              Owner: <span className="font-semibold text-slate-900 dark:text-slate-100">{pendingAction.token.user_email}</span>
            </p>
            <p>
              Project: <span className="font-semibold text-slate-900 dark:text-slate-100">{pendingAction.token.project_name}</span>
            </p>
            <p>
              Role: <span className="font-semibold text-slate-900 dark:text-slate-100">{pendingAction.token.role}</span>
            </p>
          </div>
        ) : null}
      </Dialog>
    </>
  );
}
