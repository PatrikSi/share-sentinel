#!/usr/bin/env bash
set -uo pipefail

base_url="http://localhost"
host_header=""
check_compose="true"
failures=0
warnings=0

usage() {
  cat <<'EOF'
Usage: ./scripts/doctor.sh [options]

Run read-only deployment diagnostics and report every failed check instead of
stopping at the first problem.

Options:
  --url URL       Public base URL (default: http://localhost).
  --host HOST     Send an explicit Host header for a production-style router.
  --no-compose    Skip local Docker Compose checks.
  -h, --help      Show this help.
EOF
}

pass() {
  printf 'PASS  %s\n' "$*"
}

info() {
  printf 'INFO  %s\n' "$*"
}

warn() {
  warnings=$((warnings + 1))
  printf 'WARN  %s\n' "$*" >&2
}

fail() {
  failures=$((failures + 1))
  printf 'FAIL  %s\n' "$*" >&2
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    printf 'FAIL: %s requires a value\n' "$option" >&2
    usage >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      require_value "$1" "${2:-}"
      base_url="${2%/}"
      shift 2
      ;;
    --host)
      require_value "$1" "${2:-}"
      host_header="$2"
      shift 2
      ;;
    --no-compose)
      check_compose="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'FAIL: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$base_url" =~ ^https?://[^/]+(/.*)?$ ]]; then
  printf 'FAIL: --url must be an http:// or https:// URL\n' >&2
  exit 2
fi
if [[ -n "$host_header" && ! "$host_header" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
  printf 'FAIL: --host must be a hostname with an optional numeric port\n' >&2
  exit 2
fi

if ! root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd); then
  printf 'FAIL: could not resolve the repository root\n' >&2
  exit 1
fi
if ! temp_dir=$(mktemp -d) || [[ -z "$temp_dir" ]]; then
  printf 'FAIL: could not create a temporary diagnostics directory\n' >&2
  exit 1
fi
trap 'rm -rf "$temp_dir"' EXIT

curl_headers=()
if [[ -n "$host_header" ]]; then
  curl_headers=(-H "Host: $host_header")
fi

check_http() {
  local label="$1"
  local url="$2"
  local expected_content="$3"
  local headers_file="$temp_dir/${label// /-}.headers"
  local body_file="$temp_dir/${label// /-}.body"
  local status

  status=$(curl -sS --connect-timeout 3 --max-time 10 "${curl_headers[@]}" \
    -D "$headers_file" -o "$body_file" -w '%{http_code}' "$url" 2>"$temp_dir/curl-error")
  local curl_status=$?
  if ((curl_status != 0)); then
    fail "$label is unreachable at $url: $(tr '\n' ' ' <"$temp_dir/curl-error")"
    return
  fi
  if [[ "$status" != "200" ]]; then
    fail "$label returned HTTP $status at $url"
    return
  fi
  if [[ -n "$expected_content" ]] && ! grep -Fq "$expected_content" "$body_file"; then
    fail "$label returned HTTP 200 but the response did not contain the expected marker"
    return
  fi
  pass "$label returned HTTP 200"

  if [[ "$label" == "API liveness" ]]; then
    if grep -Eiq '^x-request-id:[[:space:]]*[^[:space:]]+' "$headers_file"; then
      pass "API health response includes X-Request-ID"
    else
      warn "API health response has no X-Request-ID; verify requests reach the Share Sentinel API"
    fi
  fi
}

if ! command -v curl >/dev/null 2>&1; then
  fail "curl is required for route diagnostics"
else
  check_http "API liveness" "$base_url/api/healthz" '"ok":true'
  check_http "API readiness" "$base_url/api/healthz/ready" '"ok":true'
  check_http "UI shell" "$base_url/" ""
  if ui_check_output=$("$root_dir/scripts/check-ui-shell.sh" "$base_url" "$host_header" 2>&1); then
    pass "$ui_check_output"
  else
    fail "UI browser startup validation failed: $(tr '\n' ' ' <<<"$ui_check_output")"
  fi
fi

if [[ "$check_compose" == "true" ]]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    warn "Docker Compose is unavailable; local service and persistence checks were skipped"
  else
    cd "$root_dir" || exit 1
    if docker compose config --quiet >"$temp_dir/compose-config.out" 2>"$temp_dir/compose-config.err"; then
      pass "Docker Compose configuration renders"
    else
      fail "Docker Compose configuration is invalid: $(tr '\n' ' ' <"$temp_dir/compose-config.err")"
    fi

    for service in gateway db redis api worker ui; do
      mapfile -t container_ids < <(docker compose ps -q "$service" 2>/dev/null || true)
      if ((${#container_ids[@]} == 0)); then
        fail "$service container is not created"
        continue
      fi
      replica=0
      for container_id in "${container_ids[@]}"; do
        replica=$((replica + 1))
        state=$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
          "$container_id" 2>/dev/null || true)
        label="$service"
        if ((${#container_ids[@]} > 1)); then
          label="$service replica $replica"
        fi
        if [[ "$state" != "running" ]]; then
          fail "$label container state is ${state:-unknown}"
        elif [[ "$health" == "unhealthy" ]]; then
          fail "$label container is running but unhealthy"
        elif [[ "$health" == "starting" ]]; then
          warn "$label container health is still starting"
        else
          pass "$label container is running (health=${health:-unknown})"
        fi
      done
    done

    db_size=$(docker compose exec -T db psql -XAt -U share_sentinel -d share_sentinel \
      -c "SELECT pg_size_pretty(pg_database_size(current_database()));" 2>/dev/null || true)
    if [[ -n "$db_size" ]]; then
      info "Postgres database size: $db_size"
    else
      warn "Postgres size query failed; inspect database logs and credentials"
    fi

    queue_length=$(docker compose exec -T redis redis-cli --raw XLEN ingest_jobs 2>/dev/null || true)
    if [[ "$queue_length" =~ ^[0-9]+$ ]]; then
      info "Redis ingest stream length: $queue_length"
    else
      warn "Redis ingest stream query failed"
    fi

    artifact_usage=$(docker compose exec -T api sh -c 'df -Pk /artifacts | tail -1' 2>/dev/null || true)
    if [[ -n "$artifact_usage" ]]; then
      info "Artifact filesystem (1K blocks): $artifact_usage"
    else
      warn "Artifact filesystem usage could not be read from the API container"
    fi
  fi
fi

printf '\nDiagnostics complete: failures=%d warnings=%d\n' "$failures" "$warnings"
if ((failures > 0)); then
  exit 1
fi
