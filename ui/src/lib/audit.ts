export type AuditActorFields = {
  actor_email?: string | null;
  actor_user_id?: string | null;
  actor_token_id?: string | null;
  actor_token_name?: string | null;
};

export function formatAuditActor(event: AuditActorFields): string {
  const owner = event.actor_email || event.actor_user_id;
  if (event.actor_token_id) {
    const token = event.actor_token_name || event.actor_token_id;
    return owner ? `API token ${token} · owner ${owner}` : `API token ${token}`;
  }
  return owner || "System";
}

export function formatAuditExportTruncationWarning(
  exportTruncated: boolean,
  exportRowCount: number | null,
  exportRowLimit: number | null,
): string | null {
  if (!exportTruncated) return null;
  const boundedCount = exportRowCount ?? exportRowLimit;
  const downloaded = boundedCount === null
    ? "The downloaded file reached the server export limit"
    : `The downloaded file contains the first ${boundedCount.toLocaleString()} matching events`;
  return `${downloaded}, and additional events matched. Narrow the search or project filter and export again, or use the archival pipeline for larger history.`;
}
