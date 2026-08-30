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
});
