import { describe, expect, it } from "vitest";

import {
  directPermissionEntryLabel,
  isDirectPermissionAssessment,
  mergeAccessEvidenceDetails,
  presentAccessEvidence,
  type AccessEvidenceDetail,
} from "@/lib/access-evidence";

describe("access evidence presentation", () => {
  it("keeps partial coverage separate from the evidence outcome", () => {
    const presentation = presentAccessEvidence({
      label: "Read observed",
      outcome: "observed",
      assessment_state: "partial",
      coverage: "bounded_sample",
      direct_permissions_assessed: false,
    });

    expect(presentation.label).toBe("Read observed");
    expect(presentation.stateLabel).toBe("Partial");
    expect(presentation.coverageLabel).toBe("Bounded Sample");
    expect(presentation.tone).toBe("warning");
    expect(presentation.directPermissionsLabel).toBe("Provider permission evidence not assessed");
  });

  it("does not mistake capability probes for direct permissions", () => {
    expect(isDirectPermissionAssessment({ kind: "capability_probe", semantics: "observed_operation" })).toBe(false);
    expect(isDirectPermissionAssessment({ kind: "direct_permission" })).toBe(true);
    expect(isDirectPermissionAssessment({ semantics: "smb_windows_acl_v1", permission_surface: "smb_filesystem_dacl" })).toBe(true);
    expect(isDirectPermissionAssessment({ semantics: "sharepoint_graph_permission_v1", permission_surface: "sharepoint_graph_permissions" })).toBe(true);
  });

  it("uses provider-stable principal identifiers when no display value exists", () => {
    expect(directPermissionEntryLabel({ principal: { provider_principal_id: "sid:S-1-5-21" } })).toBe("sid:S-1-5-21");
  });

  it("renders missing normalized summaries as unavailable rather than denied", () => {
    const presentation = presentAccessEvidence(null);
    expect(presentation.label).toBe("Evidence not available");
    expect(presentation.stateLabel).toBe("Not assessed");
    expect(presentation.tone).toBe("neutral");
  });

  it("presents the persisted compact permission summary without inventing an outcome", () => {
    const presentation = presentAccessEvidence({
      evidence_available: true,
      status: "complete",
      assessment_count: 1,
      entry_count: 2,
      comparable: false,
      negative_conclusion_supported: false,
    });
    expect(presentation.label).toBe("Permission evidence available");
    expect(presentation.stateLabel).toBe("Permission assessment complete");
    expect(presentation.coverageLabel).toBe("Non-comparable Scope");
    expect(presentation.directPermissionsLabel).toBe("2 provider permission entries");
  });

  it("keeps capability observations distinct from direct permissions", () => {
    const presentation = presentAccessEvidence({
      evidence_available: true,
      status: "observed_capabilities",
      direct_permissions: {},
      capability_observations: {
        evidence_available: true,
        attempted: 4,
        allowed: ["tree_connect", "list", "read_file"],
        denied: ["create_file"],
        inconclusive: [],
        complete: true,
        method: "smb_protocol_probe",
      },
    });

    expect(presentation.label).toBe("Read observed");
    expect(presentation.stateLabel).toBe("Capability checks complete");
    expect(presentation.directPermissionsLabel).toBe("Provider permission evidence not assessed");
    expect(presentation.directPermissionsAssessed).toBe(false);
    expect(presentation.detail).toContain("not an effective-permissions calculation");
  });

  it("does not hide a failed direct-permission assessment behind positive capability evidence", () => {
    const presentation = presentAccessEvidence({
      evidence_available: true,
      status: "failed",
      assessment_count: 1,
      entry_count: 0,
      direct_permissions: { evidence_available: true, status: "failed", assessment_count: 1, entry_count: 0 },
      capability_observations: { evidence_available: true, allowed: ["read_file"] },
    });

    expect(presentation.label).toBe("Read observed");
    expect(presentation.directPermissionsLabel).toBe("Provider permission assessment failed · 0 entries recorded");
  });

  it("reports direct permission scope independently when both evidence dimensions exist", () => {
    const presentation = presentAccessEvidence({
      evidence_available: true,
      status: "complete",
      assessment_count: 1,
      entry_count: 3,
      comparable: true,
      direct_permissions: {
        evidence_available: true,
        status: "complete",
        assessment_count: 1,
        entry_count: 3,
        comparable: true,
      },
      capability_observations: {
        evidence_available: true,
        attempted: 2,
        allowed: ["tree_connect", "list"],
        complete: false,
        partial: true,
      },
    });

    expect(presentation.label).toBe("List observed");
    expect(presentation.stateLabel).toBe("Capability checks partial");
    expect(presentation.coverageLabel).toBe("Comparable Scope");
    expect(presentation.directPermissionsLabel).toBe("3 provider permission entries");
    expect(presentation.directPermissionsAssessed).toBe(true);
  });

  it("merges cursor pages without duplicating assessments or permission entries", () => {
    const firstPage: AccessEvidenceDetail = {
      overall: { evidence_available: true, assessment_count: 2, entry_count: 3 },
      assessments: [{
        id: 10,
        semantics: "smb_windows_acl_v1",
        entries: [{ id: 100, entry_key: "first", normalized_rights: ["read"] }],
      }],
      provenance: { pagination: { entries_truncated: true, next_entry_id: 100, assessments_truncated: true, next_assessment_id: 10 } },
    };
    const nextPage: AccessEvidenceDetail = {
      overall: { evidence_available: true, assessment_count: 2, entry_count: 3 },
      assessments: [
        {
          id: 10,
          semantics: "smb_windows_acl_v1",
          entries: [
            { id: 100, entry_key: "first", normalized_rights: ["read"] },
            { id: 101, entry_key: "second", normalized_rights: ["write"] },
          ],
        },
        { id: 11, semantics: "smb_windows_acl_v1", entries: [{ id: 102, entry_key: "third" }] },
      ],
      provenance: { pagination: { entries_truncated: false, next_entry_id: null, assessments_truncated: false, next_assessment_id: null } },
    };

    const merged = mergeAccessEvidenceDetails(firstPage, nextPage);
    expect(merged.assessments).toHaveLength(2);
    expect(merged.assessments[0].entries).toHaveLength(2);
    expect(merged.assessments.flatMap((assessment) => assessment.entries || []).map((entry) => entry.id)).toEqual([100, 101, 102]);
    expect(merged.provenance?.pagination?.entries_truncated).toBe(false);
  });
});
