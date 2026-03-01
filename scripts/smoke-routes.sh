#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://localhost}"

assert_contains() {
  local haystack="$1"
  local needle="$2"
  local message="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "FAIL: $message"
    echo "Expected to find: $needle"
    echo "In: $haystack"
    exit 1
  fi
}

request_with_retries() {
  local url="$1"
  local method="${2:-GET}"
  local body="${3:-}"
  local content_type="${4:-application/json}"
  local expected_csv="$5"
  local out_file="$6"
  local max_attempts="${7:-30}"
  local sleep_seconds="${8:-2}"

  local status=""
  for _ in $(seq 1 "$max_attempts"); do
    if [[ "$method" == "GET" ]]; then
      status=$(curl -sS -o "$out_file" -w "%{http_code}" "$url" || true)
    else
      status=$(curl -sS -o "$out_file" -w "%{http_code}" -X "$method" -H "content-type: $content_type" -d "$body" "$url" || true)
    fi

    IFS=',' read -r -a expected_codes <<<"$expected_csv"
    for code in "${expected_codes[@]}"; do
      if [[ "$status" == "$code" ]]; then
        echo "$status"
        return 0
      fi
    done

    sleep "$sleep_seconds"
  done

  echo "$status"
  return 1
}

ui_status=$(request_with_retries "$base_url/projects" "GET" "" "" "200" /tmp/share_sentinel_ui.out)
ui_body=$(cat /tmp/share_sentinel_ui.out 2>/dev/null || true)
assert_contains "$ui_status" "200" "UI /projects should return 200"
assert_contains "$ui_body" "<!doctype html>" "UI should return app shell"

api_health_status=$(request_with_retries "$base_url/api/healthz" "GET" "" "" "200" /tmp/share_sentinel_health.out)
api_health_body=$(cat /tmp/share_sentinel_health.out 2>/dev/null || true)
assert_contains "$api_health_status" "200" "API /api/healthz should return 200"
assert_contains "$api_health_body" "\"ok\":true" "API health payload should include ok=true"

# Detect nginx HTML 404 responses for API routes.
api_login_status=$(request_with_retries "$base_url/api/auth/login" "POST" '{"email":"missing@example.com","password":"bad"}' "application/json" "401,403,422,429" /tmp/share_sentinel_login.out)
api_login_body=$(cat /tmp/share_sentinel_login.out 2>/dev/null || true)
if [[ "$api_login_body" == *"<h1>404 Not Found</h1>"* ]]; then
  echo "FAIL: API login route returned nginx 404 HTML"
  exit 1
fi
if [[ "$api_login_status" != "401" && "$api_login_status" != "403" && "$api_login_status" != "422" && "$api_login_status" != "429" ]]; then
  echo "FAIL: API login route returned unexpected status: $api_login_status"
  echo "$api_login_body"
  exit 1
fi

echo "Smoke route checks passed for $base_url"
