import { describe, expect, it } from "vitest";

import { deriveSharePointAssessment } from "@/lib/inventory-assessment";

describe("deriveSharePointAssessment item drill-down", () => {
  it("keeps the item drill-down for a folder-only library", () => {
    const assessment = deriveSharePointAssessment({
      scope: "resource",
      metadata: {
        enumeration_status: "complete",
        content_state: "populated",
        file_count: 0,
        folder_count: 3,
        item_count: 3,
      },
    });

    expect(assessment.canViewItems).toBe(true);
  });

  it("uses the collected row count when provider counts are incomplete", () => {
    const assessment = deriveSharePointAssessment({
      scope: "resource",
      metadata: { file_count: 0 },
      itemCount: 2,
    });

    expect(assessment.canViewItems).toBe(true);
  });

  it("does not offer an empty resource or endpoint drill-down", () => {
    const emptyResource = deriveSharePointAssessment({
      scope: "resource",
      metadata: { file_count: 0, folder_count: 0, item_count: 0 },
    });
    const endpoint = deriveSharePointAssessment({
      scope: "endpoint",
      metadata: { file_count: 1, folder_count: 1, item_count: 2 },
    });

    expect(emptyResource.canViewItems).toBe(false);
    expect(endpoint.canViewItems).toBe(false);
  });
});
