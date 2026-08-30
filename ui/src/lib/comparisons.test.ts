import { describe, expect, it } from "vitest";

import {
  canRetryComparisonFindings,
  canRetryMaterializedComparison,
  canCreateMaterializedComparison,
  comparisonCompatibilityTone,
  emptyResourceChangesDescription,
  comparisonRunId,
  comparisonSummaryCounts,
  itemChangeCopy,
  resourceChangeKey,
  type ProjectComparison,
  type ResourceComparisonChange,
} from "@/lib/comparisons";

describe("comparison truthfulness helpers", () => {
  it("only enables stateful comparison creation after an operator role is confirmed", () => {
    expect(canCreateMaterializedComparison("operator", "ready")).toBe(true);
    expect(canCreateMaterializedComparison("admin", "ready")).toBe(true);
    expect(canCreateMaterializedComparison("viewer", "ready")).toBe(false);
    expect(canCreateMaterializedComparison("operator", "loading")).toBe(false);
    expect(canCreateMaterializedComparison(null, "error")).toBe(false);
  });

  it("never presents an uncomputed item delta as zero", () => {
    const notComputed = { state: "not_computed", exact: false, counts: null, total: null, before_count: 10, after_count: 10 } as const;
    expect(itemChangeCopy(notComputed)).toContain("not computed");
    expect(itemChangeCopy(notComputed)).not.toContain("0 added");
  });

  it("renders the nested item-count contract returned by the API", () => {
    const computed = {
      state: "computed",
      exact: true,
      counts: {
        added: 2,
        removed: 1,
        moved: 3,
        renamed: 4,
        metadata_changed: 5,
        permission_changed: 6,
        indeterminate: 0,
      },
      total: 21,
      before_count: 100,
      after_count: 101,
    } as const;
    const copy = itemChangeCopy(computed);
    expect(copy).toContain("2 added");
    expect(copy).toContain("4 renamed");
    expect(copy).toContain("6 permission changed");
    expect(copy).not.toContain("unknown");
    expect(copy).not.toContain("not computed");
    expect(itemChangeCopy({ ...computed, exact: false })).toContain("bounded evidence");
  });

  it("treats dimension compatibility independently", () => {
    expect(comparisonCompatibilityTone({
      status: "partial",
      structural_interpretable: true,
      content_interpretable: false,
      access_interpretable: false,
      reasons: ["Collection methods differ"],
    })).toBe("warning");
    expect(comparisonCompatibilityTone({
      status: "incompatible",
      structural_interpretable: false,
      content_interpretable: false,
      access_interpretable: false,
      reasons: [],
    })).toBe("error");
    expect(comparisonCompatibilityTone({
      status: "partial",
      structural_interpretable: true,
      content_interpretable: true,
      access_interpretable: true,
      direct_permissions_interpretable: false,
      reasons: ["Provider permission evidence is incomplete"],
    })).toBe("warning");
    expect(comparisonCompatibilityTone({
      status: "compatible",
      structural_interpretable: true,
      content_interpretable: true,
      access_interpretable: true,
      capability_interpretable: false,
      direct_permissions_interpretable: true,
      reasons: ["Capability assessment scopes differ"],
    })).toBe("warning");
    expect(comparisonCompatibilityTone({
      status: "compatible",
      structural_interpretable: true,
      content_interpretable: true,
      access_interpretable: true,
      identity_applicable: true,
      identity_scope_exact: false,
      capability_interpretable: true,
      direct_permissions_interpretable: true,
      reasons: ["Only location-bound identity is available"],
    })).toBe("warning");
    expect(comparisonCompatibilityTone({
      status: "compatible",
      structural_interpretable: true,
      content_interpretable: true,
      access_interpretable: true,
      identity_applicable: false,
      identity_scope_exact: false,
      capability_applicable: false,
      capability_interpretable: false,
      direct_permissions_interpretable: true,
      reasons: [],
    })).toBe("success");
    expect(comparisonCompatibilityTone({
      status: "compatible",
      structural_interpretable: true,
      content_interpretable: true,
      access_interpretable: true,
      capability_interpretable: true,
      direct_permissions_interpretable: true,
      reasons: [],
    })).toBe("success");
  });

  it("accepts nested and compatibility run identifiers", () => {
    const nested = { id: "comparison", state: "complete", current_run: { id: "current" } } as ProjectComparison;
    const flat = { id: "comparison", state: "complete", current_run_id: "current" } as ProjectComparison;
    expect(comparisonRunId(nested, "current")).toBe("current");
    expect(comparisonRunId(flat, "current")).toBe("current");
  });

  it("builds a deterministic row key when the API does not expose an id", () => {
    const change = {
      change_type: "changed",
      provider: "smb",
      change_categories: ["access"],
      structural_state: "unchanged",
      access_state: "changed",
      content_state: "not_computed",
      before: { endpoint_key: "server", provider_resource_id: "share-id", name: "Finance" },
      after: { endpoint_key: "server", provider_resource_id: "share-id", name: "Finance" },
    } satisfies ResourceComparisonChange;
    expect(resourceChangeKey(change, 0)).toBe(resourceChangeKey(change, 0));
    expect(resourceChangeKey(change, 0)).toContain("share-id");
  });

  it("tolerates queued and legacy comparisons with an empty or partial summary", () => {
    expect(comparisonSummaryCounts({})).toEqual({
      appeared: 0,
      disappeared: 0,
      changed: 0,
      indeterminate: 0,
      total: 0,
      published: false,
    });
    expect(comparisonSummaryCounts({ appeared: 1, disappeared: 2 })).toMatchObject({
      appeared: 1,
      disappeared: 2,
      published: false,
    });
    expect(comparisonSummaryCounts({ appeared: 1, disappeared: 2, changed: 3, indeterminate: 4, total: 10 }).published).toBe(true);
  });

  it("does not describe computed empty item history as uncomputed", () => {
    expect(emptyResourceChangesDescription({ item_churn_computed: true }, false)).toContain("was computed independently");
    expect(emptyResourceChangesDescription({ item_churn_computed: true }, false)).not.toContain("was not computed");
    expect(emptyResourceChangesDescription({ item_churn_computed: false }, false)).toContain("was not computed");
    expect(emptyResourceChangesDescription({}, false)).toContain("was not recorded");
    expect(emptyResourceChangesDescription({ item_churn_computed: true }, true)).toContain("filters");
  });

  it("exposes comparison finding recovery only for degraded complete comparisons and write roles", () => {
    const comparison = {
      id: "comparison-a",
      state: "complete",
      summary: { findings_evaluation: { state: "degraded", attempt_count: 3 } },
    } as ProjectComparison;
    expect(canRetryComparisonFindings(comparison, "operator")).toBe(true);
    expect(canRetryComparisonFindings(comparison, "admin")).toBe(true);
    expect(canRetryComparisonFindings(comparison, "viewer")).toBe(false);
    expect(canRetryComparisonFindings({ ...comparison, state: "running" }, "admin")).toBe(false);
    expect(canRetryComparisonFindings({ ...comparison, summary: { findings_evaluation: { state: "retrying" } } }, "admin")).toBe(false);
  });

  it("exposes materialized comparison retry only for failed comparisons and write roles", () => {
    const failed = { id: "comparison-a", state: "failed" } as ProjectComparison;
    expect(canRetryMaterializedComparison(failed, "operator")).toBe(true);
    expect(canRetryMaterializedComparison(failed, "admin")).toBe(true);
    expect(canRetryMaterializedComparison(failed, "viewer")).toBe(false);
    expect(canRetryMaterializedComparison({ ...failed, state: "queued" }, "admin")).toBe(false);
    expect(canRetryMaterializedComparison(null, "admin")).toBe(false);
  });
});
