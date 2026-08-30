import { describe, expect, it } from "vitest";

import {
  canManageFindings,
  canManageSources,
  evidenceTrustCopy,
  findingSeverityRank,
  formatDuration,
  humanizeMonitoringValue,
} from "@/lib/monitoring";

describe("monitoring presentation", () => {
  it("keeps finding severity ordering stable", () => {
    expect(findingSeverityRank("critical")).toBeGreaterThan(findingSeverityRank("high"));
    expect(findingSeverityRank("high")).toBeGreaterThan(findingSeverityRank("medium"));
  });

  it("does not overstate bounded or indeterminate evidence", () => {
    expect(evidenceTrustCopy("bounded")).toContain("bounded");
    expect(evidenceTrustCopy("indeterminate")).toContain("insufficient");
  });

  it("uses distinct permissions for findings and source configuration", () => {
    expect(canManageFindings("operator")).toBe(true);
    expect(canManageSources("operator")).toBe(false);
    expect(canManageSources("admin")).toBe(true);
  });

  it("formats monitoring labels and durations", () => {
    expect(humanizeMonitoringValue("accepted_risk")).toBe("Accepted Risk");
    expect(formatDuration(7_200)).toBe("2h");
    expect(formatDuration(null)).toBe("Unknown");
  });
});
