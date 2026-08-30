"""Shared API contract for materialized comparison algorithm versions."""

COMPARISON_ALGORITHM_VERSION = "resource-evidence-v3"
LEGACY_COMPARISON_ALGORITHM_WARNING = (
    "This comparison was produced by a legacy algorithm and may be incomplete. "
    "Do not treat it as authoritative; recreate the same baseline/current pair with the current algorithm."
)


def comparison_algorithm_is_current(value: object) -> bool:
    return str(value or "") == COMPARISON_ALGORITHM_VERSION
