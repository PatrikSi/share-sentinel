import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetchBlob } from "@/lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetchBlob export metadata", () => {
  it("exposes bounded-export response headers to callers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("audit-data", {
      status: 200,
      headers: {
        "content-disposition": "attachment; filename=events.csv",
        "content-type": "text/csv",
        "x-export-row-count": "5000",
        "x-export-row-limit": "5000",
        "x-export-truncated": "true",
      },
    })));

    const result = await apiFetchBlob("/settings/audit/export");

    expect(result.filename).toBe("events.csv");
    expect(result.exportTruncated).toBe(true);
    expect(result.exportRowCount).toBe(5000);
    expect(result.exportRowLimit).toBe(5000);
  });

  it("does not infer truncation from absent or malformed headers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("audit-data", {
      status: 200,
      headers: {
        "x-export-row-count": "5000rows",
        "x-export-row-limit": "-1",
      },
    })));

    const result = await apiFetchBlob("/settings/audit/export");

    expect(result.exportTruncated).toBe(false);
    expect(result.exportRowCount).toBeNull();
    expect(result.exportRowLimit).toBeNull();
  });
});
