import { useEffect, useId, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { FindingAssigneeCandidate } from "@/lib/monitoring";

export const KEEP_ASSIGNEE_VALUE = "__unchanged__";

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function FindingAssigneePicker({
  projectId,
  value,
  onChange,
  allowUnchanged = false,
  disabled = false,
  compact = false,
}: {
  projectId: string;
  value: string;
  onChange: (value: string) => void;
  allowUnchanged?: boolean;
  disabled?: boolean;
  compact?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [candidates, setCandidates] = useState<FindingAssigneeCandidate[]>([]);
  const [knownCandidates, setKnownCandidates] = useState<Record<string, FindingAssigneeCandidate>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const feedbackId = useId();

  useEffect(() => {
    setQuery("");
    setDebouncedQuery("");
    setCandidates([]);
    setKnownCandidates({});
    setError(null);
  }, [projectId]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: "100" });
    if (debouncedQuery) params.set("q", debouncedQuery);
    setLoading(true);
    setError(null);
    apiFetch(`/projects/${encodeURIComponent(projectId)}/findings/assignee-candidates?${params.toString()}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        const rows: FindingAssigneeCandidate[] = Array.isArray(data?.items)
          ? data.items.filter((candidate: unknown): candidate is FindingAssigneeCandidate => {
              if (!candidate || typeof candidate !== "object") return false;
              const row = candidate as Record<string, unknown>;
              return typeof row.id === "string" && typeof row.email === "string";
            })
          : [];
        setCandidates(rows);
        setKnownCandidates((current) => {
          const next = { ...current };
          rows.forEach((candidate) => { next[candidate.id] = candidate; });
          return next;
        });
      })
      .catch((caught) => {
        if (!controller.signal.aborted && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : "Eligible assignees could not be loaded.");
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [debouncedQuery, projectId, reloadNonce]);

  const options = useMemo(() => {
    const rows = new Map<string, FindingAssigneeCandidate>();
    candidates.forEach((candidate) => rows.set(candidate.id, candidate));
    if (value && value !== KEEP_ASSIGNEE_VALUE && knownCandidates[value]) rows.set(value, knownCandidates[value]);
    return [...rows.values()].sort((left, right) => left.email.localeCompare(right.email));
  }, [candidates, knownCandidates, value]);
  const unresolvedCurrent = value && value !== KEEP_ASSIGNEE_VALUE && !options.some((candidate) => candidate.id === value);

  return (
    <fieldset className={`finding-assignee-picker ${compact ? "is-compact" : ""}`} disabled={disabled}>
      <legend>Assignee</legend>
      <label>
        <span>Find eligible analyst</span>
        <input
          aria-describedby={feedbackId}
          maxLength={320}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") event.preventDefault(); }}
          placeholder="Search project member email"
          type="search"
          value={query}
        />
      </label>
      <label>
        <span>Assign to</span>
        <select aria-busy={loading} onChange={(event) => onChange(event.target.value)} value={value}>
          {allowUnchanged ? <option value={KEEP_ASSIGNEE_VALUE}>Keep assignment unchanged</option> : null}
          <option value="">Unassigned</option>
          {unresolvedCurrent ? <option value={value}>Current assigned analyst</option> : null}
          {options.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.email}</option>)}
        </select>
      </label>
      <div aria-live="polite" className="finding-assignee-feedback" id={feedbackId}>
        {!error ? loading ? <span>Loading eligible analysts…</span> : debouncedQuery && candidates.length === 0 ? <span>No eligible project member matches this email search.</span> : <span>{candidates.length.toLocaleString()} eligible analyst{candidates.length === 1 ? "" : "s"} in this result.</span> : null}
        {error ? <span role="alert">{error} <button onClick={() => setReloadNonce((value) => value + 1)} type="button">Retry</button></span> : null}
      </div>
    </fieldset>
  );
}
