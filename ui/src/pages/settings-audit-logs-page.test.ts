import { describe, expect, it } from "vitest";

import { formatAuditActor, formatAuditExportTruncationWarning } from "@/lib/audit";

describe("formatAuditActor", () => {
  it("keeps the exact API token and owning user visible", () => {
    expect(formatAuditActor({
      actor_email: "owner@example.com",
      actor_user_id: "user-id",
      actor_token_id: "token-id",
      actor_token_name: "nightly collector",
    })).toBe("API token nightly collector · owner owner@example.com");
  });

  it("falls back to retained identifiers after parent deletion", () => {
    expect(formatAuditActor({
      actor_email: null,
      actor_user_id: null,
      actor_token_id: "retained-token-id",
      actor_token_name: null,
    })).toBe("API token retained-token-id");
  });

  it("labels events without an actor as system activity", () => {
    expect(formatAuditActor({
      actor_email: null,
      actor_user_id: null,
      actor_token_id: null,
      actor_token_name: null,
    })).toBe("System");
  });
});

describe("formatAuditExportTruncationWarning", () => {
  it("explains that a capped download is incomplete and how to narrow it", () => {
    expect(formatAuditExportTruncationWarning(true, 5000, 5000)).toBe(
      "The downloaded file contains the first 5,000 matching events, and additional events matched. Narrow the search or project filter and export again, or use the archival pipeline for larger history.",
    );
  });

  it("stays hidden for complete downloads", () => {
    expect(formatAuditExportTruncationWarning(false, 12, 5000)).toBeNull();
  });

  it("has a safe fallback when a proxy strips the row-count headers", () => {
    expect(formatAuditExportTruncationWarning(true, null, null)).toContain("server export limit");
  });
});
