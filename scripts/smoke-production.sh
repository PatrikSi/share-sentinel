#!/usr/bin/env bash
set -euo pipefail

base_url="${1:?usage: smoke-production.sh BASE_URL HOSTNAME [ADMIN_EMAIL]}"
hostname="${2:?usage: smoke-production.sh BASE_URL HOSTNAME [ADMIN_EMAIL]}"
admin_email="${3:-admin@example.com}"
admin_password="${SHARE_SENTINEL_SMOKE_PASSWORD:?set SHARE_SENTINEL_SMOKE_PASSWORD}"
curl_args=(--connect-timeout 3 --max-time 10)
temp_dir=$(mktemp -d)

cleanup() {
  rm -rf "$temp_dir"
}
trap cleanup EXIT

request_until_status() {
  local url="$1"
  local output="$2"
  local status=""
  for _ in $(seq 1 60); do
    status=$(curl "${curl_args[@]}" -sS -o "$output" -w "%{http_code}" -H "Host: $hostname" "$url" || true)
    if [[ "$status" == "200" ]]; then
      echo "$status"
      return 0
    fi
    sleep 1
  done
  echo "$status"
  return 1
}

health_headers="$temp_dir/health-headers.txt"
health_status=$(request_until_status "${base_url%/}/api/healthz" "$temp_dir/health.json")
jq -e '.ok == true' "$temp_dir/health.json" >/dev/null
curl "${curl_args[@]}" -fsS -o /dev/null -D "$health_headers" -H "Host: $hostname" "${base_url%/}/api/healthz"
grep -qi '^X-Content-Type-Options: nosniff' "$health_headers"

ready_status=$(request_until_status "${base_url%/}/api/healthz/ready" "$temp_dir/ready.json")
jq -e '.ok == true and (.checks | to_entries | all(.value == "ok"))' "$temp_dir/ready.json" >/dev/null

docs_status=$(curl "${curl_args[@]}" -sS -o "$temp_dir/docs.json" -w "%{http_code}" -H "Host: $hostname" "${base_url%/}/api/docs")
[[ "$docs_status" == "404" ]]

ui_status=$(curl "${curl_args[@]}" -sS -o "$temp_dir/ui.html" -w "%{http_code}" -H "Host: $hostname" "${base_url%/}/projects")
[[ "$ui_status" == "200" ]]
grep -qi '<!doctype html>' "$temp_dir/ui.html"

login_payload=$(jq -nc --arg email "$admin_email" --arg password "$admin_password" '{email: $email, password: $password}')
login_status=$(curl "${curl_args[@]}" -sS -o "$temp_dir/login.json" -D "$temp_dir/login-headers.txt" -w "%{http_code}" -H "Host: $hostname" -H "content-type: application/json" --data-binary @- "${base_url%/}/api/auth/login" <<<"$login_payload")
[[ "$login_status" == "200" ]]
secure_cookies=$(grep -ic '^Set-Cookie:.*; Secure' "$temp_dir/login-headers.txt")
[[ "$secure_cookies" == "3" ]]

cors_status=$(curl "${curl_args[@]}" -sS -o /dev/null -D "$temp_dir/cors-headers.txt" -w "%{http_code}" -X OPTIONS -H "Host: $hostname" -H "Origin: https://$hostname" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type,x-csrf-token" "${base_url%/}/api/auth/login")
[[ "$cors_status" == "200" ]]
grep -qi "^access-control-allow-origin: https://$hostname" "$temp_dir/cors-headers.txt"

echo "Production smoke passed: health=$health_status ready=$ready_status docs=$docs_status ui=$ui_status secure_cookies=$secure_cookies cors=$cors_status"
