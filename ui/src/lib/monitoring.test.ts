import { describe, expect, it } from "vitest";

import {
  canManageFindings,
  canManageSources,
  evidenceTrustCopy,
  findingEvidenceFacts,
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

  it("renders real worker finding evidence as bounded safe facts", () => {
    const summary = findingEvidenceFacts({
      allowed_capabilities: ["create_file", "create_directory"],
      probe_method: "smb_create_probe",
      complete: true,
      before: {
        provider_metadata: { authorization: "Bearer must-never-render" },
        access_token: "must-never-render",
      },
    });
    const references = findingEvidenceFacts(
      { run_id: "3d1c53f8-ff85-4e7a-aacd-bbee6bb0c7e2", resource_id: 42, access_token: "must-never-render" },
      "references",
    );

    expect(summary.find((fact) => fact.key === "allowed_capabilities")?.value).toContain("create_file");
    expect(summary.find((fact) => fact.key === "probe_method")?.value).toBe("smb_create_probe");
    expect(summary.find((fact) => fact.key === "before")?.withheld).toBe(true);
    expect(references.find((fact) => fact.key === "run_id")?.withheld).toBe(false);
    expect(JSON.stringify({ summary, references })).not.toContain("must-never-render");
  });

  it("withholds unknown comparison snapshot values instead of serializing them", () => {
    const facts = findingEvidenceFacts({
      change_type: "permission_changed",
      categories: ["access", "permission_evidence"],
      structural_state: "unchanged",
      access_state: "changed",
      content_state: "unchanged",
      after: { password: "must-never-render", name: "Finance" },
    });

    expect(facts.find((fact) => fact.key === "change_type")?.value).toBe("permission_changed");
    expect(facts.find((fact) => fact.key === "after")?.value).toContain("raw values withheld");
    expect(JSON.stringify(facts)).not.toContain("must-never-render");
  });
});
