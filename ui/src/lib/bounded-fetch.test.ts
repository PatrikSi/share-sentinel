import { describe, expect, it } from "vitest";

import { errorMessageFromBody } from "@/lib/bounded-fetch";

describe("API problem detail presentation", () => {
  it("preserves actionable structured conflict messages", () => {
    const message = errorMessageFromBody(JSON.stringify({
      detail: {
        code: "FINDING_REVISION_CONFLICT",
        message: "The finding changed after it was loaded; refresh and retry.",
        current_revision: 7,
      },
    }), 409, "request-123");

    expect(message).toContain("changed after it was loaded");
    expect(message).toContain("FINDING_REVISION_CONFLICT");
    expect(message).toContain("request-123");
  });

  it("preserves atomic bulk revision conflict guidance", () => {
    const message = errorMessageFromBody(JSON.stringify({
      detail: {
        code: "FINDING_BULK_REVISION_CONFLICT",
        message: "One or more findings changed after they were loaded; refresh and retry.",
        conflicts: [{ finding_id: "finding-a", current_revision: 4 }],
      },
    }), 409);

    expect(message).toContain("changed after they were loaded");
    expect(message).toContain("FINDING_BULK_REVISION_CONFLICT");
    expect(message).not.toContain("finding-a");
  });

  it.each([
    [409, "MONITORING_RUN_SUPERSEDED", "newer successful snapshot"],
    [429, "MONITORING_RETRY_RATE_LIMITED", "wait before retrying"],
    [409, "COMPARISON_FINDINGS_NOT_RETRYABLE", "completed comparison"],
  ])("preserves actionable monitoring recovery errors without serializing extra detail", (status, code, guidance) => {
    const message = errorMessageFromBody(JSON.stringify({
      detail: {
        code,
        message: `Recovery blocked: ${guidance}.`,
        superseding_run_id: "run-safe-reference",
        internal_error: "Bearer must-never-render",
      },
    }), status);

    expect(message).toContain(code);
    expect(message).toContain(guidance);
    expect(message).not.toContain("run-safe-reference");
    expect(message).not.toContain("must-never-render");
  });
});
