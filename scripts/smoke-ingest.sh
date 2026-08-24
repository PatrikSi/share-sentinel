#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-http://localhost}"
admin_email="${2:-admin@example.com}"
admin_password="${SHARE_SENTINEL_SMOKE_PASSWORD:?set SHARE_SENTINEL_SMOKE_PASSWORD}"
api_base="${base_url%/}/api"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
default_artifact="$script_dir/../examples/sample-artifact.json"
sample_artifact="${3:-$default_artifact}"
smoke_timeout_seconds="${SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS:-300}"
curl_common_args=(--connect-timeout 5 --max-time 30)
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
      curl "${curl_common_args[@]}" -sS -o /dev/null -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" \
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

if [[ ! -f "$sample_artifact" ]]; then
  echo "FAIL: artifact does not exist: $sample_artifact"
  exit 1
fi
if [[ ! "$smoke_timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "FAIL: SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS must be a positive integer"
  exit 2
fi
curl_upload_args=(--connect-timeout 5 --max-time "$smoke_timeout_seconds")

artifact_filename=$(basename "$sample_artifact")
artifact_content_type="application/json"
if [[ "$artifact_filename" == *.gz ]]; then
  artifact_content_type="application/gzip"
elif [[ "$artifact_filename" == *.ndjson || "$artifact_filename" == *.jsonl ]]; then
  artifact_content_type="application/x-ndjson"
fi

expected_endpoints=3
expected_resources=3
expected_items=6
expected_errors=1
is_default_artifact="false"
if [[ "$sample_artifact" -ef "$default_artifact" ]]; then
  is_default_artifact="true"
else
  validator="$script_dir/validate-ndjson.py"
  if [[ ! -x "$validator" ]]; then
    echo "FAIL: custom artifact validator is not executable: $validator"
    exit 1
  fi
  validation_summary=$("$validator" --summary-only --json "$sample_artifact")
  expected_endpoints=$(jq -er '.endpoints' <<<"$validation_summary")
  expected_resources=$(jq -er '.resources' <<<"$validation_summary")
  expected_items=$(jq -er '.items' <<<"$validation_summary")
  expected_errors=$(jq -er '.errors' <<<"$validation_summary")
  for expected_count in "$expected_endpoints" "$expected_resources" "$expected_items" "$expected_errors"; do
    if [[ ! "$expected_count" =~ ^[0-9]+$ ]]; then
      echo "FAIL: custom artifact run_end counts must be non-negative integers"
      exit 2
    fi
  done
fi

login_payload=$(jq -nc --arg email "$admin_email" --arg password "$admin_password" '{email: $email, password: $password}')
login_response=$(curl "${curl_common_args[@]}" -fsS -c "$cookies" -H "content-type: application/json" --data-binary @- "$api_base/auth/login" <<<"$login_payload")
jq -e --arg email "$admin_email" '.user.email == $email and .user.is_sysadmin == true' <<<"$login_response" >/dev/null

csrf=$(awk '$6 == "share_sentinel_csrf" {print $7}' "$cookies")
if [[ -z "$csrf" ]]; then
  echo "FAIL: login did not set the CSRF cookie"
  exit 1
fi

project_name="Publication smoke $(date +%s)-$RANDOM"
project_payload=$(jq -nc --arg name "$project_name" '{name: $name}')
project_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -c "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" --data-binary @- "$api_base/projects" <<<"$project_payload")
project_id=$(jq -er '.id' <<<"$project_response")

run_payload=$(jq -nc --arg filename "$artifact_filename" '{
  name: "Synthetic publication ingest",
  description: "Automated release or capacity validation",
  target_scope: {source: "smoke-test", artifact: $filename}
}')
run_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -c "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" --data-binary @- "$api_base/projects/$project_id/runs" <<<"$run_payload")
run_id=$(jq -er '.id' <<<"$run_response")

upload_response=$(curl "${curl_upload_args[@]}" -fsS -b "$cookies" -c "$cookies" \
  -H "x-csrf-token: $csrf" \
  -H "content-type: $artifact_content_type" \
  -H "x-artifact-filename: $artifact_filename" \
  --data-binary "@$sample_artifact" \
  "$api_base/projects/$project_id/runs/$run_id/artifact")
jq -e '.ok == true' <<<"$upload_response" >/dev/null

status=""
started_at=$(date +%s)
while (( $(date +%s) - started_at < smoke_timeout_seconds )); do
  run_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/runs/$run_id")
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

jq -e \
  --argjson endpoints "$expected_endpoints" \
  --argjson resources "$expected_resources" \
  --argjson items "$expected_items" \
  --argjson errors "$expected_errors" \
  '.summary.endpoints == $endpoints and .summary.resources == $resources and .summary.items == $items and .summary.errors == $errors' \
  <<<"$run_response" >/dev/null
stats_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/inventory/stats")
jq -e --argjson endpoints "$expected_endpoints" --argjson resources "$expected_resources" \
  '.endpoints == $endpoints and .shares == $resources' <<<"$stats_response" >/dev/null
if [[ "$is_default_artifact" == "true" ]]; then
  jq -e '.files == 4 and .directories == 2' <<<"$stats_response" >/dev/null
  endpoints_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/runs/$run_id/endpoints")
  jq -e '(.items | map(select(.endpoint_key == "192.0.2.10:445" and .smb_signing == "required")) | length) == 1' <<<"$endpoints_response" >/dev/null
  items_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/inventory/items?q=retention.pdf&run_ids=$run_id")
  jq -e '(.items | length) == 1 and .items[0].size_bytes == 2048 and .items[0].mtime == "2026-01-15T09:30:00+00:00"' <<<"$items_response" >/dev/null
  sharepoint_resources_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/inventory/resources?provider=sharepoint&resource_type=sharepoint_library&exposure=USER_VISIBLE&run_ids=$run_id")
  jq -e '(.items | length) == 1 and .items[0].provider_resource_id == "b!synthetic-drive-id" and .items[0].access_level == "list_only"' <<<"$sharepoint_resources_response" >/dev/null
  sharepoint_items_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/inventory/items?provider=sharepoint&exposure=USER_VISIBLE&run_ids=$run_id")
  jq -e '(.items | length) == 2 and (.items | map(select(.provider_item_id == "synthetic-file-id" and .path == "/Records/quarterly-review.docx" and .deleted == false)) | length) == 1' <<<"$sharepoint_items_response" >/dev/null
  errors_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/projects/$project_id/runs/$run_id/errors")
  jq -e '(.items | length) == 1 and .items[0].code == "SYNTHETIC_PARTIAL_SCAN"' <<<"$errors_response" >/dev/null
fi
detail_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" "$api_base/settings/projects/$project_id")
jq -e '.run_count == 1 and .artifact_count == 1 and .run_status_counts.COMPLETE == 1' <<<"$detail_response" >/dev/null

wrong_payload='{"confirm_name":"Wrong project"}'
wrong_status=$(curl "${curl_common_args[@]}" -sS -o /dev/null -w "%{http_code}" -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$wrong_payload")
if [[ "$wrong_status" != "400" ]]; then
  echo "FAIL: wrong project confirmation returned $wrong_status instead of 400"
  exit 1
fi

delete_payload=$(jq -nc --arg confirm_name "$project_name" '{confirm_name: $confirm_name}')
delete_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -H "content-type: application/json" -H "x-csrf-token: $csrf" -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$delete_payload")
jq -e '.ok == true and .deleted_run_count == 1 and .deleted_artifact_count == 1 and (.artifact_delete_failures | length == 0)' <<<"$delete_response" >/dev/null
project_id=""

echo "Ingest smoke passed: run=$run_id endpoints=$expected_endpoints shares=$expected_resources items=$expected_items errors=$expected_errors"
