import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ProviderBadge } from "@/components/provider-context";
import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import {
  canManageFindings,
  evidenceTrustCopy,
  findingTone,
  formatMonitoringTimestamp,
  humanizeMonitoringValue,
  type Finding,
  type FindingActivity,
  type FindingOccurrence,
  type FindingPolicy,
  type FindingSeverity,
  type FindingStatus,
  type FindingSummary,
} from "@/lib/monitoring";

const PAGE_LIMIT = 50;
const FINDING_STATUSES: FindingStatus[] = ["open", "acknowledged", "accepted_risk", "resolved"];
const FINDING_SEVERITIES: FindingSeverity[] = ["critical", "high", "medium", "low", "info"];
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function readParam(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) || "";
}

function normalizeStatus(value: string): FindingStatus | "" {
  return FINDING_STATUSES.includes(value as FindingStatus) ? (value as FindingStatus) : "";
}

function normalizeSeverity(value: string): FindingSeverity | "" {
  return FINDING_SEVERITIES.includes(value as FindingSeverity) ? (value as FindingSeverity) : "";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function localDateTimeValue(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function EvidenceDetail({ finding }: { finding: Finding }) {
  const limitations = finding.evidence?.limitations || [];
  const references = finding.evidence?.refs || [];
  return (
    <section aria-labelledby="finding-evidence-title" className="monitoring-detail-section">
      <div className="monitoring-detail-heading">
        <div>
          <h3 id="finding-evidence-title">Detection evidence</h3>
          <p>{finding.evidence?.summary || "No evidence summary was recorded."}</p>
        </div>
        <span className={`evidence-state is-${finding.evidence?.state || "indeterminate"}`}>
          {humanizeMonitoringValue(finding.evidence?.state || "indeterminate")}
        </span>
      </div>
      <p className="monitoring-trust-copy">{evidenceTrustCopy(finding.evidence?.state || "indeterminate")}</p>
      {limitations.length > 0 ? (
        <div className="monitoring-limitations">
          <strong>Limitations</strong>
          <ul>{limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
        </div>
      ) : null}
      {references.length > 0 ? (
        <details className="monitoring-raw-details">
          <summary>Evidence references ({references.length.toLocaleString()})</summary>
          <pre>{JSON.stringify(references, null, 2)}</pre>
        </details>
      ) : null}
    </section>
  );
}

function FindingHistory({ finding }: { finding: Finding }) {
  const [occurrences, setOccurrences] = useState<FindingOccurrence[]>([]);
  const [occurrenceCursor, setOccurrenceCursor] = useState<string | null>(null);
  const [occurrenceLoading, setOccurrenceLoading] = useState(true);
  const [occurrenceError, setOccurrenceError] = useState<string | null>(null);
  const [occurrenceNonce, setOccurrenceNonce] = useState(0);
  const [activity, setActivity] = useState<FindingActivity[]>([]);
  const [activityCursor, setActivityCursor] = useState<string | null>(null);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [activityNonce, setActivityNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setOccurrenceLoading(true);
    setOccurrenceError(null);
    setOccurrences([]);
    setOccurrenceCursor(null);
    apiFetch(`/projects/${encodeURIComponent(finding.project_id)}/findings/${encodeURIComponent(finding.id)}/occurrences?limit=20`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setOccurrences(Array.isArray(data?.items) ? data.items as FindingOccurrence[] : []);
        setOccurrenceCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
      })
      .catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) setOccurrenceError(caught instanceof Error ? caught.message : "Finding occurrences could not be loaded."); })
      .finally(() => !controller.signal.aborted && setOccurrenceLoading(false));
    return () => controller.abort();
  }, [finding.id, finding.project_id, occurrenceNonce]);

  useEffect(() => {
    const controller = new AbortController();
    setActivityLoading(true);
    setActivityError(null);
    setActivity([]);
    setActivityCursor(null);
    apiFetch(`/projects/${encodeURIComponent(finding.project_id)}/findings/${encodeURIComponent(finding.id)}/activity?limit=20`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setActivity(Array.isArray(data?.items) ? data.items as FindingActivity[] : []);
        setActivityCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
      })
      .catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) setActivityError(caught instanceof Error ? caught.message : "Finding activity could not be loaded."); })
      .finally(() => !controller.signal.aborted && setActivityLoading(false));
    return () => controller.abort();
  }, [activityNonce, finding.id, finding.project_id, finding.revision]);

  async function loadMoreOccurrences() {
    if (!occurrenceCursor || occurrenceLoading) return;
    setOccurrenceLoading(true); setOccurrenceError(null);
    try {
      const data = await apiFetch(`/projects/${encodeURIComponent(finding.project_id)}/findings/${encodeURIComponent(finding.id)}/occurrences?limit=20&cursor=${encodeURIComponent(occurrenceCursor)}`);
      setOccurrences((rows) => [...rows, ...((data?.items || []) as FindingOccurrence[])]);
      setOccurrenceCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
    } catch (caught) { setOccurrenceError(caught instanceof Error ? caught.message : "Additional occurrences could not be loaded."); }
    finally { setOccurrenceLoading(false); }
  }

  async function loadMoreActivity() {
    if (!activityCursor || activityLoading) return;
    setActivityLoading(true); setActivityError(null);
    try {
      const data = await apiFetch(`/projects/${encodeURIComponent(finding.project_id)}/findings/${encodeURIComponent(finding.id)}/activity?limit=20&cursor=${encodeURIComponent(activityCursor)}`);
      setActivity((rows) => [...rows, ...((data?.items || []) as FindingActivity[])]);
      setActivityCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
    } catch (caught) { setActivityError(caught instanceof Error ? caught.message : "Additional activity could not be loaded."); }
    finally { setActivityLoading(false); }
  }

  return (
    <section aria-labelledby="finding-history-title" className="monitoring-detail-section">
      <div className="monitoring-detail-heading"><div><h3 id="finding-history-title">Evidence and decision history</h3><p>Detection occurrences and human lifecycle actions are separate, durable timelines.</p></div></div>
      <div className="finding-history-grid">
        <div>
          <div className="finding-history-heading"><h4>Occurrences</h4><span>{occurrences.length.toLocaleString()} loaded</span></div>
          {occurrenceError ? <StatusBanner tone="error" title="Occurrence history unavailable"><p>{occurrenceError}</p><button className="mt-2 rounded-md border border-current px-3 py-1.5 text-xs font-semibold" onClick={() => setOccurrenceNonce((value) => value + 1)} type="button">Retry occurrences</button></StatusBanner> : null}
          {occurrenceLoading && occurrences.length === 0 ? <p className="finding-history-loading" role="status">Loading occurrences…</p> : null}
          {!occurrenceLoading && !occurrenceError && occurrences.length === 0 ? <p className="finding-history-empty">No occurrence rows are available. Do not infer that the finding was never observed.</p> : null}
          {occurrences.length > 0 ? <ol className="finding-history-list">{occurrences.map((occurrence) => <li key={occurrence.id}><span className={`evidence-state is-${occurrence.evidence_state}`}>{humanizeMonitoringValue(occurrence.evidence_state)}</span><div><strong>{formatMonitoringTimestamp(occurrence.observed_at)}</strong><small>Policy {occurrence.policy_id} · v{occurrence.policy_version}</small><div className="monitoring-action-row"><Link to={`/projects/${finding.project_id}/runs/${occurrence.run_id}`}>Run evidence</Link>{occurrence.comparison_id ? <Link to={`/projects/${finding.project_id}/comparisons/${occurrence.comparison_id}`}>Comparison</Link> : null}</div></div></li>)}</ol> : null}
          {occurrenceCursor ? <button className="inventory-button-secondary mt-2" disabled={occurrenceLoading} onClick={() => void loadMoreOccurrences()} type="button">{occurrenceLoading ? "Loading…" : "Load more occurrences"}</button> : null}
        </div>
        <div>
          <div className="finding-history-heading"><h4>Analyst activity</h4><span>{activity.length.toLocaleString()} loaded</span></div>
          {activityError ? <StatusBanner tone="error" title="Analyst activity unavailable"><p>{activityError}</p><button className="mt-2 rounded-md border border-current px-3 py-1.5 text-xs font-semibold" onClick={() => setActivityNonce((value) => value + 1)} type="button">Retry activity</button></StatusBanner> : null}
          {activityLoading && activity.length === 0 ? <p className="finding-history-loading" role="status">Loading analyst activity…</p> : null}
          {!activityLoading && !activityError && activity.length === 0 ? <p className="finding-history-empty">No finding lifecycle actions have been recorded.</p> : null}
          {activity.length > 0 ? <ol className="finding-history-list">{activity.map((event) => {
            const metadata = event.metadata || {};
            const transition = [metadata.old_status, metadata.new_status].filter((value) => typeof value === "string").map((value) => humanizeMonitoringValue(String(value))).join(" → ");
            const actor = event.actor_user_id ? `User ${event.actor_user_id}` : event.actor_token_id ? `Token ${event.actor_token_id}` : "System";
            return <li key={event.id}><span className="finding-activity-mark" aria-hidden="true" /><div><strong>{humanizeMonitoringValue(event.action)}</strong><small>{formatMonitoringTimestamp(event.ts)} · {actor}</small>{transition ? <p>{transition}</p> : null}{typeof metadata.note === "string" && metadata.note ? <p>{metadata.note}</p> : null}</div></li>;
          })}</ol> : null}
          {activityCursor ? <button className="inventory-button-secondary mt-2" disabled={activityLoading} onClick={() => void loadMoreActivity()} type="button">{activityLoading ? "Loading…" : "Load more activity"}</button> : null}
        </div>
      </div>
    </section>
  );
}

type FindingDetailProps = {
  canManage: boolean;
  permissionsReady: boolean;
  permissionError: string | null;
  finding: Finding | null;
  loading: boolean;
  error: string | null;
  busy: boolean;
  onReload: () => void;
  onUpdate: (payload: Record<string, unknown>) => Promise<void>;
};

function FindingDetail({ canManage, permissionsReady, permissionError, finding, loading, error, busy, onReload, onUpdate }: FindingDetailProps) {
  const [status, setStatus] = useState<FindingStatus>("open");
  const [expiry, setExpiry] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!finding) return;
    setStatus(finding.status);
    setExpiry(localDateTimeValue(finding.accepted_risk_expires_at));
    setNote("");
  }, [finding?.id, finding?.revision]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!finding) return;
    const payload: Record<string, unknown> = { status, note: note.trim() || undefined, revision: finding.revision };
    if (status === "accepted_risk") {
      const parsed = new Date(expiry);
      if (!expiry || Number.isNaN(parsed.getTime()) || parsed.getTime() <= Date.now()) return;
      payload.accepted_risk_expires_at = parsed.toISOString();
    }
    await onUpdate(payload);
  }

  if (loading) return <StatePanel description="Loading current lifecycle state and detection evidence." title="Loading finding" />;
  if (error) {
    return <StatePanel actions={<button className="inventory-button-primary" onClick={onReload} type="button">Retry finding</button>} description={`${error} Retrying this read is safe.`} title="Finding unavailable" tone="error" />;
  }
  if (!finding) return <StatePanel description="Choose a finding from the queue to inspect evidence and lifecycle history without losing filters." title="Select a finding" />;

  const acceptedRiskInvalid = status === "accepted_risk" && (!expiry || new Date(expiry).getTime() <= Date.now());
  return (
    <article className="monitoring-detail-card">
      <header className="monitoring-detail-header">
        <div>
          <div className="monitoring-badges">
            <span className={`finding-severity is-${findingTone(finding.severity)}`}>{humanizeMonitoringValue(finding.severity)}</span>
            <span className={`finding-status is-${finding.status}`}>{humanizeMonitoringValue(finding.status)}</span>
          </div>
          <h2>{finding.title}</h2>
          <p>{finding.description || "No policy description was recorded."}</p>
        </div>
        <small>Revision {finding.revision}</small>
      </header>

      <dl className="monitoring-fact-grid">
        <div><dt>Resource</dt><dd>{finding.resource_name || finding.resource_identity_key || "Not recorded"}</dd></div>
        <div><dt>Provider</dt><dd>{humanizeMonitoringValue(finding.provider)}</dd></div>
        <div><dt>Policy</dt><dd>{finding.policy_id} · v{finding.policy_version}</dd></div>
        <div><dt>Occurrences</dt><dd>{finding.occurrence_count.toLocaleString()}</dd></div>
        <div><dt>First seen</dt><dd>{formatMonitoringTimestamp(finding.first_seen_at)}</dd></div>
        <div><dt>Last seen</dt><dd>{formatMonitoringTimestamp(finding.last_seen_at)}</dd></div>
        <div><dt>Assignee</dt><dd>{finding.assignee_user_id || "Unassigned"}</dd></div>
        <div><dt>Finding ID</dt><dd className="font-mono" title={finding.id}>{finding.id}</dd></div>
      </dl>

      <EvidenceDetail finding={finding} />

      <FindingHistory finding={finding} />

      <section aria-labelledby="finding-context-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading"><div><h3 id="finding-context-title">Investigation context</h3><p>Open the immutable run or comparison evidence that last produced this finding.</p></div></div>
        <div className="monitoring-action-row">
          {finding.latest_run_id ? <Link className="inventory-button-secondary" to={`/projects/${finding.project_id}/runs/${finding.latest_run_id}`}>Open latest run</Link> : null}
          {finding.latest_comparison_id ? <Link className="inventory-button-secondary" to={`/projects/${finding.project_id}/comparisons/${finding.latest_comparison_id}`}>Open comparison</Link> : null}
          {finding.resource_name ? <Link className="inventory-button-secondary" to={`/projects/${finding.project_id}/inventory?${new URLSearchParams({ q: finding.resource_name }).toString()}`}>Find in inventory</Link> : null}
        </div>
      </section>

      <section aria-labelledby="finding-lifecycle-title" className="monitoring-detail-section">
        <div className="monitoring-detail-heading">
          <div><h3 id="finding-lifecycle-title">Lifecycle action</h3><p>Status changes and analyst notes are recorded in the audit trail.</p></div>
        </div>
        {canManage ? (
          <form className="monitoring-action-form" onSubmit={(event) => void submit(event)}>
            <label>Status<select disabled={busy} onChange={(event) => setStatus(event.target.value as FindingStatus)} value={status}>{FINDING_STATUSES.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>
            {status === "accepted_risk" ? <label>Risk acceptance expires<input aria-invalid={acceptedRiskInvalid} disabled={busy} min={localDateTimeValue(new Date(Date.now() + 60_000).toISOString())} onChange={(event) => setExpiry(event.target.value)} required type="datetime-local" value={expiry} /></label> : null}
            <label className="monitoring-action-note">Analyst note<textarea disabled={busy} maxLength={4000} onChange={(event) => setNote(event.target.value)} placeholder="Explain the decision or handoff context" rows={3} value={note} /></label>
            {acceptedRiskInvalid ? <p className="monitoring-validation" role="alert">Accepted risk requires an expiry time in the future.</p> : null}
            <div className="monitoring-action-row"><button className="inventory-button-primary" disabled={busy || acceptedRiskInvalid} type="submit">{busy ? "Updating…" : "Apply lifecycle update"}</button></div>
          </form>
        ) : permissionError ? <StatusBanner tone="warning" title="Action permissions unavailable"><p>{permissionError} No lifecycle action is enabled until the project role can be verified.</p></StatusBanner> : permissionsReady ? <StatusBanner title="Read-only project role"><p>Operator or project admin access is required to change finding status. Evidence and current state remain available.</p></StatusBanner> : <StatePanel description="Checking whether this project role can update finding lifecycle state." title="Loading action permissions" />}
      </section>
    </article>
  );
}

export function FindingsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState<FindingStatus | "">(() => normalizeStatus(readParam("status")));
  const [severity, setSeverity] = useState<FindingSeverity | "">(() => normalizeSeverity(readParam("severity")));
  const [policyId, setPolicyId] = useState(() => readParam("policy"));
  const [sourceId, setSourceId] = useState(() => readParam("source"));
  const [query, setQuery] = useState(() => readParam("q"));
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  const [cursor, setCursor] = useState<string | null>(() => readParam("cursor") || null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>(() => cursor ? [null] : []);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState(() => readParam("finding"));
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [findings, setFindings] = useState<Finding[]>([]);
  const [summary, setSummary] = useState<FindingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [detail, setDetail] = useState<Finding | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailNonce, setDetailNonce] = useState(0);
  const [role, setRole] = useState<string | null>(null);
  const [roleReady, setRoleReady] = useState(false);
  const [roleError, setRoleError] = useState<string | null>(null);
  const [policies, setPolicies] = useState<FindingPolicy[]>([]);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationInfo, setMutationInfo] = useState<string | null>(null);
  const [bulkStatus, setBulkStatus] = useState<Exclude<FindingStatus, "accepted_risk">>("acknowledged");
  const requestProjectRef = useRef(projectId);
  requestProjectRef.current = projectId;
  const sourceIdInvalid = !!sourceId && !UUID_PATTERN.test(sourceId);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const next = new URLSearchParams(searchParams);
    status ? next.set("status", status) : next.delete("status");
    severity ? next.set("severity", severity) : next.delete("severity");
    policyId ? next.set("policy", policyId) : next.delete("policy");
    sourceId ? next.set("source", sourceId) : next.delete("source");
    debouncedQuery ? next.set("q", debouncedQuery) : next.delete("q");
    cursor ? next.set("cursor", cursor) : next.delete("cursor");
    selectedId ? next.set("finding", selectedId) : next.delete("finding");
    if (next.toString() !== searchParams.toString()) setSearchParams(next, { replace: true });
  }, [cursor, debouncedQuery, policyId, searchParams, selectedId, setSearchParams, severity, sourceId, status]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setRoleReady(false);
    setRoleError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/my-role`, { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setRole(typeof data?.role === "string" ? data.role : null))
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setRole(null);
        setRoleError(caught instanceof Error ? caught.message : "Project role could not be verified.");
      })
      .finally(() => !controller.signal.aborted && setRoleReady(true));
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setPolicyError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/finding-policies`, { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setPolicies(Array.isArray(data?.items) ? data.items as FindingPolicy[] : []))
      .catch((caught) => { if (!controller.signal.aborted && !isAbortError(caught)) setPolicyError(caught instanceof Error ? caught.message : "Finding policies could not be loaded."); });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    if (sourceIdInvalid) {
      setFindings([]);
      setSummary(null);
      setNextCursor(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (status) params.set("status", status);
    if (severity) params.set("severity", severity);
    if (policyId) params.set("policy_id", policyId);
    if (sourceId) params.set("source_id", sourceId);
    if (debouncedQuery) params.set("q", debouncedQuery);
    if (cursor) params.set("cursor", cursor);
    setLoading(true);
    setError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/findings?${params.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted || requestProjectRef.current !== projectId) return;
        const rows = Array.isArray(data?.items) ? data.items as Finding[] : [];
        setFindings(rows);
        setSummary((data?.summary || null) as FindingSummary | null);
        setNextCursor(typeof data?.next_cursor === "string" ? data.next_cursor : null);
        setSelectedIds(new Set());
        setLastLoadedAt(new Date());
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Findings could not be loaded.");
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [cursor, debouncedQuery, policyId, projectId, reloadNonce, severity, sourceId, sourceIdInvalid, status]);

  useEffect(() => {
    if (!projectId || !selectedId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetailError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/findings/${encodeURIComponent(selectedId)}`, { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setDetail(data as Finding))
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) {
          setDetail(null);
          setDetailError(caught instanceof Error ? caught.message : "Finding detail could not be loaded.");
        }
      })
      .finally(() => !controller.signal.aborted && setDetailLoading(false));
    return () => controller.abort();
  }, [detailNonce, projectId, selectedId]);

  function resetPage() {
    setCursor(null);
    setCursorHistory([]);
  }

  function toggleSelected(id: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function updateFinding(payload: Record<string, unknown>) {
    if (!projectId || !detail) return;
    setMutationBusy(true);
    setMutationError(null);
    setMutationInfo(null);
    try {
      const updated = await apiFetch(`/projects/${encodeURIComponent(projectId)}/findings/${encodeURIComponent(detail.id)}`, { method: "PATCH", body: JSON.stringify(payload) }) as Finding;
      setDetail(updated);
      setFindings((rows) => rows.map((row) => row.id === updated.id ? updated : row));
      setMutationInfo(`Finding moved to ${humanizeMonitoringValue(updated.status)}.`);
      setReloadNonce((value) => value + 1);
    } catch (caught) {
      setMutationError(`${caught instanceof Error ? caught.message : "Finding update failed."} The update was not applied. The current finding has been reloaded; review it before retrying.`);
      setDetailNonce((value) => value + 1);
    } finally {
      setMutationBusy(false);
    }
  }

  async function bulkUpdate() {
    if (!projectId || selectedIds.size === 0) return;
    setMutationBusy(true);
    setMutationError(null);
    setMutationInfo(null);
    try {
      const data = await apiFetch(`/projects/${encodeURIComponent(projectId)}/findings/bulk`, {
        method: "POST",
        body: JSON.stringify({ finding_ids: [...selectedIds], status: bulkStatus }),
      });
      setMutationInfo(`${Number(data?.updated_count || selectedIds.size).toLocaleString()} selected finding${selectedIds.size === 1 ? "" : "s"} updated.`);
      setSelectedIds(new Set());
      setReloadNonce((value) => value + 1);
      if (selectedId) setDetailNonce((value) => value + 1);
    } catch (caught) {
      setMutationError(`${caught instanceof Error ? caught.message : "Bulk finding update failed."} The operation is atomic; no selected finding was intentionally left half-updated.`);
    } finally {
      setMutationBusy(false);
    }
  }

  const allPageSelected = findings.length > 0 && findings.every((finding) => selectedIds.has(finding.id));
  const canManage = canManageFindings(role);
  const statusCounts = useMemo(() => FINDING_STATUSES.map((value) => ({ value, count: summary?.[value] || 0 })), [summary]);

  return (
    <section className="monitoring-workspace">
      <header className="monitoring-page-header">
        <div><p>Continuous monitoring</p><h1>Findings</h1><span>Prioritize exposure and access changes, inspect evidence, and record accountable lifecycle decisions.</span></div>
        <div className="monitoring-freshness"><strong>{lastLoadedAt ? `Updated ${lastLoadedAt.toLocaleTimeString()}` : "Not loaded"}</strong><span>Server-filtered · {PAGE_LIMIT} per page</span></div>
      </header>

      <section aria-label="Finding status summary" className="monitoring-summary-strip">
        {statusCounts.map(({ value, count }) => <button aria-pressed={status === value} className={status === value ? "is-active" : ""} key={value} onClick={() => { setStatus(status === value ? "" : value); resetPage(); }} type="button"><span>{humanizeMonitoringValue(value)}</span><strong>{count.toLocaleString()}</strong></button>)}
        <div><span>Total</span><strong>{(summary?.total || 0).toLocaleString()}</strong></div>
      </section>

      <section aria-label="Finding filters" className="monitoring-filter-bar">
        <label>Search<input onChange={(event) => { setQuery(event.target.value); resetPage(); }} placeholder="Title, resource, or policy" type="search" value={query} /></label>
        <label>Severity<select onChange={(event) => { setSeverity(normalizeSeverity(event.target.value)); resetPage(); }} value={severity}><option value="">All severities</option>{FINDING_SEVERITIES.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>
        <label>Status<select onChange={(event) => { setStatus(normalizeStatus(event.target.value)); resetPage(); }} value={status}><option value="">All statuses</option>{FINDING_STATUSES.map((value) => <option key={value} value={value}>{humanizeMonitoringValue(value)}</option>)}</select></label>
        <label>Policy<select disabled={!!policyError} onChange={(event) => { setPolicyId(event.target.value); resetPage(); }} value={policyId}><option value="">All policies</option>{policies.map((policy) => <option key={policy.id} value={policy.id}>{policy.title}</option>)}</select></label>
        {(status || severity || query || policyId || sourceId) ? <button className="inventory-button-secondary" onClick={() => { setStatus(""); setSeverity(""); setQuery(""); setPolicyId(""); setSourceId(""); resetPage(); }} type="button">Clear filters</button> : null}
      </section>

      {policyError ? <StatusBanner tone="warning" title="Policy filter unavailable"><p>{policyError} The finding queue can still be filtered by severity, status, and search.</p></StatusBanner> : null}
      {sourceIdInvalid ? <StatusBanner tone="warning" title="Source scope is invalid"><p>The source parameter is not a complete UUID. No findings request is sent until the scope is cleared.</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setSourceId("")} type="button">Clear source scope</button></StatusBanner> : sourceId ? <StatusBanner title="Source-scoped findings"><p>Only findings registered to source <code>{sourceId}</code> are included.</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => { setSourceId(""); resetPage(); }} type="button">Show all sources</button></StatusBanner> : null}

      {mutationError ? <StatusBanner tone="error" title="Finding action failed"><p>{mutationError}</p></StatusBanner> : null}
      {mutationInfo ? <StatusBanner tone="success" title="Finding updated"><p>{mutationInfo}</p></StatusBanner> : null}

      <div className="monitoring-split-layout">
        <section aria-busy={loading} aria-labelledby="findings-queue-title" className="monitoring-queue">
          <header className="monitoring-queue-header"><div><h2 id="findings-queue-title">Analyst queue</h2><p>Selection applies only to the {findings.length.toLocaleString()} rows on this page.</p></div><span>Page {cursorHistory.length + 1}</span></header>
          {canManage && selectedIds.size > 0 ? <div className="monitoring-bulk-bar" role="region" aria-label="Bulk finding actions"><strong>{selectedIds.size.toLocaleString()} selected on this page</strong><select aria-label="Bulk status" disabled={mutationBusy} onChange={(event) => setBulkStatus(event.target.value as Exclude<FindingStatus, "accepted_risk">)} value={bulkStatus}><option value="acknowledged">Acknowledge</option><option value="resolved">Resolve</option><option value="open">Reopen</option></select><button className="inventory-button-primary" disabled={mutationBusy} onClick={() => void bulkUpdate()} type="button">{mutationBusy ? "Updating…" : "Apply to selected"}</button><button className="inventory-button-secondary" onClick={() => setSelectedIds(new Set())} type="button">Clear selection</button></div> : null}
          {error ? <StatePanel actions={<button className="inventory-button-primary" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry findings</button>} description={`${error} No new queue state is shown.`} title="Findings unavailable" tone="error" /> : null}
          {loading ? <div aria-label="Loading findings" className="inventory-skeleton" role="status">{Array.from({ length: 8 }, (_, index) => <span key={index} />)}</div> : null}
          {!sourceIdInvalid && !loading && !error && findings.length === 0 ? <StatePanel description={status || severity || debouncedQuery || policyId || sourceId ? "No findings match the current server-side filters." : "No findings have been created for this project yet. A successful comparable collection will evaluate the built-in monitoring policies."} title="No findings in view" /> : null}
          {!loading && !error && findings.length > 0 ? (
            <div className="monitoring-table-scroll">
              <table className="monitoring-table">
                <caption className="sr-only">Security findings for this project</caption>
                <thead><tr><th><input aria-label="Select all findings on this page" checked={allPageSelected} onChange={() => setSelectedIds(allPageSelected ? new Set() : new Set(findings.map((finding) => finding.id)))} type="checkbox" /></th><th>Severity</th><th>Finding</th><th>Status</th><th>Resource</th><th>Evidence</th><th>Last seen</th></tr></thead>
                <tbody>{findings.map((finding) => <tr className={selectedId === finding.id ? "is-selected" : ""} key={finding.id}><td><input aria-label={`Select ${finding.title}`} checked={selectedIds.has(finding.id)} onChange={() => toggleSelected(finding.id)} type="checkbox" /></td><td><span className={`finding-severity is-${findingTone(finding.severity)}`}>{humanizeMonitoringValue(finding.severity)}</span></td><td><button className="monitoring-row-title" onClick={() => setSelectedId(finding.id)} type="button"><strong>{finding.title}</strong><span>{finding.policy_id} · {finding.occurrence_count.toLocaleString()} occurrence{finding.occurrence_count === 1 ? "" : "s"}</span></button></td><td><span className={`finding-status is-${finding.status}`}>{humanizeMonitoringValue(finding.status)}</span></td><td><span className="monitoring-resource-name" title={finding.resource_name || finding.resource_identity_key || undefined}>{finding.resource_name || finding.resource_identity_key || "Not recorded"}</span><ProviderBadge provider={finding.provider || "unknown"} /></td><td><span className={`evidence-state is-${finding.evidence?.state || "indeterminate"}`}>{humanizeMonitoringValue(finding.evidence?.state || "indeterminate")}</span></td><td>{formatMonitoringTimestamp(finding.last_seen_at)}</td></tr>)}</tbody>
              </table>
            </div>
          ) : null}
          <footer className="monitoring-pagination"><span>Opaque cursor pagination preserves stable server order.</span><nav aria-label="Finding pages"><button disabled={cursorHistory.length === 0 || loading} onClick={() => { const previous = cursorHistory[cursorHistory.length - 1] ?? null; setCursorHistory((values) => values.slice(0, -1)); setCursor(previous); }} type="button">Previous</button><strong aria-current="page">{cursorHistory.length + 1}</strong><button disabled={!nextCursor || loading} onClick={() => { setCursorHistory((values) => [...values, cursor]); setCursor(nextCursor); }} type="button">Next</button></nav></footer>
        </section>

        <aside aria-label="Selected finding details" className="monitoring-detail-pane">
          <FindingDetail busy={mutationBusy} canManage={canManage} error={detailError} finding={detail} loading={detailLoading} onReload={() => setDetailNonce((value) => value + 1)} onUpdate={updateFinding} permissionError={roleError} permissionsReady={roleReady} />
        </aside>
      </div>
    </section>
  );
}
