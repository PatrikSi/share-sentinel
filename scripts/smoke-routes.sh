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

ui_status=$(curl -sS -o /tmp/share_sentinel_ui.out -w "%{http_code}" "$base_url/projects")
ui_body=$(cat /tmp/share_sentinel_ui.out)
assert_contains "$ui_status" "200" "UI /projects should return 200"
assert_contains "$ui_body" "<!doctype html>" "UI should return app shell"

api_health_status=$(curl -sS -o /tmp/share_sentinel_health.out -w "%{http_code}" "$base_url/api/healthz")
api_health_body=$(cat /tmp/share_sentinel_health.out)
assert_contains "$api_health_status" "200" "API /api/healthz should return 200"
assert_contains "$api_health_body" "\"ok\":true" "API health payload should include ok=true"

# Detect nginx HTML 404 responses for API routes.
api_login_status=$(curl -sS -o /tmp/share_sentinel_login.out -w "%{http_code}" -X POST "$base_url/api/auth/login" -H "content-type: application/json" -d '{"email":"missing@example.com","password":"bad"}')
api_login_body=$(cat /tmp/share_sentinel_login.out)
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
