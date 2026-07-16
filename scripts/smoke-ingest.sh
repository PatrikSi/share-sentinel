#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://localhost}"
admin_email="${2:-admin@example.com}"
admin_password="${SHARE_SENTINEL_SMOKE_PASSWORD:?set SHARE_SENTINEL_SMOKE_PASSWORD}"
api_base="${base_url%/}/api"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
sample_artifact="$script_dir/../examples/sample-artifact.json"
temp_dir=$(mktemp -d)
cookies="$temp_dir/cookies.txt"
project_id=""
project_name=""
csrf=""

cleanup() {
  local cleanup_payload=""
  if [[ -n "$project_id" && -n "$project_name" && -n "$csrf" && -f "$cookies" ]]; then
    cleanup_payload=$(jq -nc --arg confirm_name "$project_name" '{confirm_name: $confirm_name}' 2>/dev/null || true)
    if [[ -n "$cleanup_payload" ]]; then
      curl -sS -o /dev/null -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" \
        -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$cleanup_payload" || true
    fi
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT

for command in curl jq; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "FAIL: $command is required"
    exit 1
  fi
done

login_payload=$(jq -nc --arg email "$admin_email" --arg password "$admin_password" '{email: $email, password: $password}')
login_response=$(curl -fsS -c "$cookies" -H "content-type: application/json" --data-binary @- "$api_base/auth/login" <<<"$login_payload")
jq -e --arg email "$admin_email" '.user.email == $email and .user.is_sysadmin == true' <<<"$login_response" >/dev/null

csrf=$(awk '$6 == "share_sentinel_csrf" {print $7}' "$cookies")
if [[ -z "$csrf" ]]; then
  echo "FAIL: login did not set the CSRF cookie"
  exit 1
fi

project_name="Publication smoke $(date +%s)-$RANDOM"
project_payload=$(jq -nc --arg name "$project_name" '{name: $name}')
project_response=$(curl -fsS -b "$cookies" -c "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" --data-binary @- "$api_base/projects" <<<"$project_payload")
project_id=$(jq -er '.id' <<<"$project_response")

run_payload='{"name":"Synthetic publication ingest","description":"Automated release validation","target_scope":{"source":"tracked-fixture"}}'
run_response=$(curl -fsS -b "$cookies" -c "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" --data-binary @- "$api_base/projects/$project_id/runs" <<<"$run_payload")
run_id=$(jq -er '.id' <<<"$run_response")

upload_response=$(curl -fsS -b "$cookies" -c "$cookies" -H "x-csrf-token: $csrf" -F "file=@$sample_artifact;type=application/json" "$api_base/projects/$project_id/runs/$run_id/artifact")
jq -e '.ok == true' <<<"$upload_response" >/dev/null

status=""
for _ in $(seq 1 90); do
  run_response=$(curl -fsS -b "$cookies" "$api_base/projects/$project_id/runs/$run_id")
  status=$(jq -r '.status' <<<"$run_response")
  if [[ "$status" == "COMPLETE" || "$status" == "FAILED" ]]; then
    break
  fi
  sleep 1
done

if [[ "$status" != "COMPLETE" ]]; then
  echo "FAIL: run ended in status $status"
  jq . <<<"$run_response"
  exit 1
fi

jq -e '.summary.endpoints == 2 and .summary.resources == 2 and .summary.items == 4 and .summary.errors == 1' <<<"$run_response" >/dev/null
stats_response=$(curl -fsS -b "$cookies" "$api_base/projects/$project_id/inventory/stats")
jq -e '.endpoints == 2 and .shares == 2 and .files == 3 and .directories == 1' <<<"$stats_response" >/dev/null
errors_response=$(curl -fsS -b "$cookies" "$api_base/projects/$project_id/runs/$run_id/errors")
jq -e '(.items | length) == 1 and .items[0].code == "SYNTHETIC_PARTIAL_SCAN"' <<<"$errors_response" >/dev/null
detail_response=$(curl -fsS -b "$cookies" "$api_base/settings/projects/$project_id")
jq -e '.run_count == 1 and .artifact_count == 1 and .run_status_counts.COMPLETE == 1' <<<"$detail_response" >/dev/null

wrong_payload='{"confirm_name":"Wrong project"}'
wrong_status=$(curl -sS -o /dev/null -w "%{http_code}" -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$wrong_payload")
if [[ "$wrong_status" != "400" ]]; then
  echo "FAIL: wrong project confirmation returned $wrong_status instead of 400"
  exit 1
fi

delete_payload=$(jq -nc --arg confirm_name "$project_name" '{confirm_name: $confirm_name}')
delete_response=$(curl -fsS -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$delete_payload")
jq -e '.ok == true and .deleted_run_count == 1 and .deleted_artifact_count == 1 and (.artifact_delete_failures | length == 0)' <<<"$delete_response" >/dev/null
project_id=""

echo "Ingest smoke passed: run=$run_id endpoints=2 shares=2 items=4 errors=1"
