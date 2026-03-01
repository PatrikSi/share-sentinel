from __future__ import annotations

import math
import threading
from collections import defaultdict

HTTP_REQUEST_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    float("inf"),
)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_requests_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_request_errors_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_request_duration_seconds_bucket: dict[tuple[str, str, float], int] = defaultdict(int)
        self._http_request_duration_seconds_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._http_request_duration_seconds_count: dict[tuple[str, str], int] = defaultdict(int)

    def reset(self) -> None:
        with self._lock:
            self._http_requests_total.clear()
            self._http_request_errors_total.clear()
            self._http_request_duration_seconds_bucket.clear()
            self._http_request_duration_seconds_sum.clear()
            self._http_request_duration_seconds_count.clear()

    def record_http_request(self, method: str, path: str, status_code: int, latency_seconds: float) -> None:
        normalized_method = _normalize_method(method)
        normalized_path = _normalize_path(path)
        normalized_status = _normalize_status(status_code)
        normalized_latency = _normalize_latency(latency_seconds)

        with self._lock:
            request_key = (normalized_method, normalized_path, normalized_status)
            self._http_requests_total[request_key] += 1

            bucket_key = (normalized_method, normalized_path)
            self._http_request_duration_seconds_sum[bucket_key] += normalized_latency
            self._http_request_duration_seconds_count[bucket_key] += 1
            for bucket in HTTP_REQUEST_DURATION_BUCKETS:
                if normalized_latency <= bucket:
                    self._http_request_duration_seconds_bucket[(normalized_method, normalized_path, bucket)] += 1

    def record_http_error(self, method: str, path: str, error: str) -> None:
        normalized_method = _normalize_method(method)
        normalized_path = _normalize_path(path)
        normalized_error = _normalize_error(error)
        with self._lock:
            self._http_request_errors_total[(normalized_method, normalized_path, normalized_error)] += 1

    def render_prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP share_sentinel_http_requests_total Total number of HTTP requests by method, path, and status code.",
                "# TYPE share_sentinel_http_requests_total counter",
            ]
            for (method, path, status), count in sorted(self._http_requests_total.items()):
                lines.append(
                    "share_sentinel_http_requests_total"
                    + _labels({"method": method, "path": path, "status": status})
                    + f" {count}"
                )

            lines.extend(
                [
                    "# HELP share_sentinel_http_request_errors_total Total number of HTTP request errors by method, path, and error type.",
                    "# TYPE share_sentinel_http_request_errors_total counter",
                ]
            )
            for (method, path, error), count in sorted(self._http_request_errors_total.items()):
                lines.append(
                    "share_sentinel_http_request_errors_total"
                    + _labels({"method": method, "path": path, "error": error})
                    + f" {count}"
                )

            lines.extend(
                [
                    "# HELP share_sentinel_http_request_duration_seconds Request latency histogram in seconds by method and path.",
                    "# TYPE share_sentinel_http_request_duration_seconds histogram",
                ]
            )

            histogram_keys = set(self._http_request_duration_seconds_sum.keys()) | set(self._http_request_duration_seconds_count.keys())
            for method, path in sorted(histogram_keys):
                for bucket in HTTP_REQUEST_DURATION_BUCKETS:
                    bucket_value = self._http_request_duration_seconds_bucket.get((method, path, bucket), 0)
                    lines.append(
                        "share_sentinel_http_request_duration_seconds_bucket"
                        + _labels({"method": method, "path": path, "le": _format_bucket(bucket)})
                        + f" {bucket_value}"
                    )
                lines.append(
                    "share_sentinel_http_request_duration_seconds_sum"
                    + _labels({"method": method, "path": path})
                    + f" {_format_float(self._http_request_duration_seconds_sum.get((method, path), 0.0))}"
                )
                lines.append(
                    "share_sentinel_http_request_duration_seconds_count"
                    + _labels({"method": method, "path": path})
                    + f" {self._http_request_duration_seconds_count.get((method, path), 0)}"
                )
        return "\n".join(lines) + "\n"


_registry = MetricsRegistry()


def record_http_request(method: str, path: str, status_code: int, latency_seconds: float) -> None:
    _registry.record_http_request(method, path, status_code, latency_seconds)


def record_http_error(method: str, path: str, error: str) -> None:
    _registry.record_http_error(method, path, error)


def render_prometheus() -> str:
    return _registry.render_prometheus()


def reset_for_tests() -> None:
    _registry.reset()


def _labels(values: dict[str, str]) -> str:
    ordered = [f'{name}="{_escape_label(value)}"' for name, value in values.items()]
    return "{" + ",".join(ordered) + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _normalize_method(value: str) -> str:
    method = str(value or "UNKNOWN").strip().upper()
    return method or "UNKNOWN"


def _normalize_path(value: str) -> str:
    path = str(value or "__unknown__").strip()
    return path or "__unknown__"


def _normalize_status(value: int) -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return "0"
    return str(max(0, parsed))


def _normalize_latency(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, parsed)


def _normalize_error(value: str) -> str:
    label = str(value or "unknown_error").strip()
    return label or "unknown_error"


def _format_bucket(bucket: float) -> str:
    if math.isinf(bucket):
        return "+Inf"
    if float(bucket).is_integer():
        return str(int(bucket))
    return f"{bucket:g}"


def _format_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
