from typing import Any


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def build_access_evidence_summary(
    permission_summary: Any,
    access_level: Any,
    access_capabilities: Any,
    exposure: Any = None,
) -> dict[str, Any]:
    """Merge access evidence dimensions without conflating their semantics."""

    direct = dict(permission_summary) if isinstance(permission_summary, dict) else {}
    capabilities = access_capabilities if isinstance(access_capabilities, dict) else {}
    observed: list[str] = []
    denied: list[str] = []
    inconclusive: list[str] = []
    attempted = 0
    for raw_name, raw_evidence in capabilities.items():
        if raw_name == "_metadata" or not isinstance(raw_evidence, dict):
            continue
        name = str(raw_name)
        status = str(raw_evidence.get("status") or "not_tested")
        attempted += _nonnegative_int(raw_evidence.get("attempted"))
        if status in {"allowed", "mixed"} or _nonnegative_int(raw_evidence.get("allowed")) > 0:
            observed.append(name)
        if status in {"denied", "mixed"} or _nonnegative_int(raw_evidence.get("denied")) > 0:
            denied.append(name)
        if status == "inconclusive" or _nonnegative_int(raw_evidence.get("inconclusive")) > 0:
            inconclusive.append(name)
    observed.sort()
    denied.sort()
    inconclusive.sort()
    metadata = capabilities.get("_metadata") if isinstance(capabilities.get("_metadata"), dict) else {}
    direct_available = direct.get("evidence_available") is True
    capability_available = attempted > 0 or bool(observed or denied or inconclusive)
    if direct_available:
        status = str(direct.get("status") or "available")
    elif capability_available:
        status = "observed_capabilities"
    else:
        status = "not_assessed"
    normalized_access_level = access_level.value if hasattr(access_level, "value") else access_level
    write_capabilities = {
        "create_file",
        "create_directory",
        "modify_file",
        "delete",
        "write_acl",
        "write_owner",
    }
    return {
        **direct,
        "status": status,
        "evidence_available": direct_available or capability_available,
        "direct_permissions": direct,
        "capability_observations": {
            "evidence_available": capability_available,
            "attempted": attempted,
            "allowed": observed,
            "denied": denied,
            "inconclusive": inconclusive,
            "writable_observed": bool(write_capabilities.intersection(observed)),
            "complete": metadata.get("complete") is True,
            "partial": metadata.get("partial") is True,
            "method": metadata.get("probe_method"),
        },
        "compatibility_access_level": normalized_access_level,
        "exposure": exposure,
        # Negative conclusions remain a property of a complete direct
        # permission assessment. Probe denials do not establish absence.
        "negative_conclusion_supported": direct.get("negative_conclusion_supported") is True,
    }
