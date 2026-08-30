import { useEffect, useRef, useState } from "react";

import { StatePanel } from "@/components/state-panel";
import { StatusBanner } from "@/components/status-banner";
import { apiFetch } from "@/lib/api";
import { humanizeEvidenceValue } from "@/lib/access-evidence";
import {
  effectiveAccessDecisionIsConclusive,
  type EffectiveAccessAnalysis,
  type EffectiveAccessPrincipal,
} from "@/lib/monitoring";

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function boundedPlaneValue(value: unknown): string {
  if (Array.isArray(value)) return value.slice(0, 5).map(boundedPlaneValue).join(", ");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .slice(0, 4)
      .map(([key, nested]) => `${humanizeEvidenceValue(key)}: ${boundedPlaneValue(nested)}`)
      .join("; ");
  }
  return String(value ?? "not recorded");
}

function planeFacts(planes: Record<string, unknown> | null | undefined): Array<{ label: string; state: string; detail: string }> {
  if (!planes) return [];
  return Object.entries(planes).map(([key, raw]) => {
    const value = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
    const state = String(value.state || value.decision || value.status || "not_recorded");
    const detailEntries = Object.entries(value)
      .filter(([field]) => !["state", "decision", "status"].includes(field))
      .sort(([left], [right]) => {
        const priority = ["reason", "subject", "interpretation", "limitations"];
        const leftRank = priority.indexOf(left);
        const rightRank = priority.indexOf(right);
        return (leftRank < 0 ? priority.length : leftRank) - (rightRank < 0 ? priority.length : rightRank);
      })
      .slice(0, 4);
    const detail = detailEntries
      .map(([field, entry]) => `${humanizeEvidenceValue(field)}: ${boundedPlaneValue(entry)}`)
      .join(" · ");
    return { label: humanizeEvidenceValue(key), state: humanizeEvidenceValue(state), detail };
  });
}

function computedSubjectLabel(planes: Record<string, unknown> | null | undefined): string | null {
  const computed = planes?.computed_effective;
  if (!computed || typeof computed !== "object") return null;
  const subject = (computed as Record<string, unknown>).subject;
  if (!subject || typeof subject !== "object") return null;
  const fields = subject as Record<string, unknown>;
  for (const key of ["subject_path", "subject_key", "subject_provider_id", "subject_kind"]) {
    const value = String(fields[key] ?? "").trim();
    if (value) return value.length > 120 ? `${value.slice(0, 117)}…` : value;
  }
  return null;
}

function principalName(principal: EffectiveAccessPrincipal): string {
  return principal.display_name || principal.email || principal.login_name || principal.principal_key || `Principal ${principal.id ?? "unknown"}`;
}

export function EffectiveAccessAnalysisSection({ projectId, runId, resourceId }: { projectId: string; runId: string; resourceId: number }) {
  const [requested, setRequested] = useState(false);
  const [analysis, setAnalysis] = useState<EffectiveAccessAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const loadMoreController = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!requested) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setAnalysis(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/resources/${resourceId}/effective-access?limit=100`, { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setAnalysis(data as EffectiveAccessAnalysis))
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Effective-access evidence could not be analyzed.");
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [projectId, reloadNonce, requested, resourceId, runId]);

  useEffect(() => () => loadMoreController.current?.abort(), []);

  async function loadMore() {
    const cursor = analysis?.principals?.next_cursor;
    if (!cursor || loadingMore) return;
    loadMoreController.current?.abort();
    const controller = new AbortController();
    loadMoreController.current = controller;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const data = await apiFetch(`/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/resources/${resourceId}/effective-access?limit=100&cursor=${encodeURIComponent(cursor)}`, { signal: controller.signal }) as EffectiveAccessAnalysis;
      if (controller.signal.aborted) return;
      setAnalysis((current) => {
        if (!current) return data;
        const rows = new Map<string, EffectiveAccessPrincipal>();
        current.principals.items.forEach((principal, index) => rows.set(String(principal.id ?? principal.principal_key ?? index), principal));
        data.principals.items.forEach((principal, index) => rows.set(String(principal.id ?? principal.principal_key ?? `new:${index}`), principal));
        return { ...current, ...data, principals: { items: [...rows.values()], next_cursor: data.principals.next_cursor || null } };
      });
    } catch (caught) {
      if (!controller.signal.aborted && !isAbortError(caught)) setLoadMoreError(caught instanceof Error ? caught.message : "Additional principals could not be loaded.");
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (loadMoreController.current === controller) loadMoreController.current = null;
    }
  }

  const facts = planeFacts(analysis?.evidence_planes);
  const limitations = analysis?.limitations || [];
  const computedSubject = computedSubjectLabel(analysis?.evidence_planes);
  return (
    <section aria-labelledby="effective-access-title" className="access-evidence-section effective-access-section">
      <div className="access-evidence-section-heading"><div><h3 id="effective-access-title">Effective-access explanation</h3><p>Correlate provider grants, observed operations, and any provider-computed decision while preserving uncertainty.</p></div>{analysis ? <span className={`evidence-state is-${analysis.analysis_state === "computed" ? "exact" : analysis.analysis_state === "bounded" ? "bounded" : "indeterminate"}`}>{humanizeEvidenceValue(analysis.analysis_state)}</span> : null}</div>
      {!requested ? <StatePanel actions={<button className="inventory-button-primary" onClick={() => setRequested(true)} type="button">Analyze stored evidence</button>} description="This read is intentionally explicit and audited. It does not contact the resource or change permissions." title="Effective access has not been analyzed in this panel" /> : null}
      {loading ? <div aria-live="polite" className="access-evidence-panel-loading" role="status"><span /><span /><span /><p>Correlating normalized permission evidence…</p></div> : null}
      {error ? <StatusBanner tone="error" title="Effective-access analysis unavailable"><p>{error}</p><p className="mt-1">No decision is being inferred. Retrying this evidence read is safe.</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry analysis</button></StatusBanner> : null}
      {analysis && !loading ? <>
        <StatusBanner tone={analysis.analysis_state === "computed" ? "success" : analysis.analysis_state === "bounded" ? "warning" : "info"} title={analysis.analysis_state === "computed" ? `Computed decision: ${humanizeEvidenceValue(analysis.decision)}${computedSubject ? ` for ${computedSubject}` : ""}` : analysis.analysis_state === "bounded" ? "Bounded access evidence only" : "Effective access is indeterminate"}>
          <p>{analysis.analysis_state === "computed" ? "The provider evidence declared effective computation for the shown scope. Verify provenance before remediation." : analysis.analysis_state === "bounded" ? "Direct grants and observed capabilities are available, but unresolved groups, inheritance, or provider semantics prevent a definitive effective decision." : "The collected evidence cannot support an allow or deny conclusion. Unknown must not be interpreted as denied."}</p>
        </StatusBanner>

        {facts.length > 0 ? <div className="effective-access-planes">{facts.map((fact) => <article key={fact.label}><span>{fact.label}</span><strong>{fact.state}</strong><p>{fact.detail || "No additional plane detail recorded."}</p></article>)}</div> : null}

        {limitations.length > 0 ? <div className="access-evidence-limitations"><strong>Global limitations</strong><ul>{limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></div> : null}

        <div className="effective-principal-heading"><div><h4>Observed principals</h4><p>Direct evidence is separate from effective decision.</p></div><span>{analysis.principals.items.length.toLocaleString()} loaded</span></div>
        {analysis.principals.items.length > 0 ? <div className="effective-principal-table-scroll"><table className="effective-principal-table"><caption className="sr-only">Principals observed in permission evidence</caption><thead><tr><th>Principal</th><th>Resolution</th><th>Direct evidence</th><th>Effective decision</th><th>Rights and limits</th></tr></thead><tbody>{analysis.principals.items.map((principal, index) => <tr key={String(principal.id ?? principal.principal_key ?? index)}><td><strong>{principalName(principal)}</strong><small>{humanizeEvidenceValue(principal.kind || principal.principal_type)}</small></td><td>{humanizeEvidenceValue(principal.resolution)}</td><td>{humanizeEvidenceValue(principal.direct_decision || principal.decision)}</td><td><span className={`evidence-state is-${effectiveAccessDecisionIsConclusive(principal.effective_decision) ? "exact" : "indeterminate"}`}>{humanizeEvidenceValue(principal.effective_decision || "unknown")}</span></td><td><p>{(principal.allow_rights || []).length ? `Allow evidence: ${principal.allow_rights?.join(", ")}` : "No normalized allow rights"}</p>{(principal.deny_rights || []).length ? <p>Deny evidence: {principal.deny_rights?.join(", ")}</p> : null}{(principal.limitations || []).length ? <small title={principal.limitations?.join(" ")}>{principal.limitations?.length} limitation{principal.limitations?.length === 1 ? "" : "s"}</small> : null}</td></tr>)}</tbody></table></div> : <StatePanel description="No normalized principal rows were returned. This is not evidence that no one has access." title="No principals available" />}
        {analysis.principals.next_cursor ? <button className="inventory-button-secondary mt-3" disabled={loadingMore} onClick={() => void loadMore()} type="button">{loadingMore ? "Loading more principals…" : "Load more principals"}</button> : null}
        {loadMoreError ? <StatusBanner tone="error" title="Additional principals unavailable"><p>{loadMoreError} Already loaded principals remain visible.</p><button className="mt-2 rounded-md border border-current px-3 py-2 text-xs font-semibold" onClick={() => void loadMore()} type="button">Retry more principals</button></StatusBanner> : null}
      </> : null}
    </section>
  );
}
