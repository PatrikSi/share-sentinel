#!/usr/bin/env bash
set -euo pipefail

trap 'status=$?; echo "FAIL: SharePoint ingest smoke failed near line ${LINENO} (exit ${status})" >&2; exit "${status}"' ERR

base_url="${1:-http://localhost}"
admin_email="${2:-admin@example.com}"
admin_password="${SHARE_SENTINEL_SMOKE_PASSWORD:?set SHARE_SENTINEL_SMOKE_PASSWORD}"
api_base="${base_url%/}/api"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
baseline_artifact="${3:-$script_dir/../examples/sample-sharepoint-baseline.ndjson}"
current_artifact="${4:-$script_dir/../examples/sample-sharepoint-current.ndjson}"
smoke_timeout_seconds="${SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS:-300}"
curl_common_args=(--connect-timeout 5 --max-time 30)
temp_dir=$(mktemp -d)
cookies="$temp_dir/cookies.txt"
project_id=""
project_name=""
csrf=""
uploaded_run_id=""

cleanup() {
  local cleanup_payload=""
  if [[ -n "$project_id" && -n "$project_name" && -n "$csrf" && -f "$cookies" ]]; then
    cleanup_payload=$(jq -nc --arg confirm_name "$project_name" '{confirm_name: $confirm_name}' 2>/dev/null || true)
    if [[ -n "$cleanup_payload" ]]; then
      curl "${curl_common_args[@]}" -sS -o /dev/null -b "$cookies" \
        -H "content-type: application/json" -H "x-csrf-token: $csrf" \
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

if [[ ! "$smoke_timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "FAIL: SHARE_SENTINEL_SMOKE_TIMEOUT_SECONDS must be a positive integer"
  exit 2
fi
for artifact in "$baseline_artifact" "$current_artifact"; do
  if [[ ! -f "$artifact" ]]; then
    echo "FAIL: artifact does not exist: $artifact"
    exit 1
  fi
  "$script_dir/validate-ndjson.py" --summary-only --json "$artifact" >/dev/null
done

login_payload=$(jq -nc --arg email "$admin_email" --arg password "$admin_password" \
  '{email: $email, password: $password}')
login_response=$(curl "${curl_common_args[@]}" -fsS -c "$cookies" \
  -H "content-type: application/json" --data-binary @- "$api_base/auth/login" <<<"$login_payload")
jq -e --arg email "$admin_email" '.user.email == $email and .user.is_sysadmin == true' \
  <<<"$login_response" >/dev/null

csrf=$(awk '$6 == "share_sentinel_csrf" {print $7}' "$cookies")
if [[ -z "$csrf" ]]; then
  echo "FAIL: login did not set the CSRF cookie"
  exit 1
fi

project_name="SharePoint diff smoke $(date +%s)-$RANDOM"
project_payload=$(jq -nc --arg name "$project_name" '{name: $name}')
project_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -c "$cookies" \
  -H "content-type: application/json" -H "x-csrf-token: $csrf" \
  --data-binary @- "$api_base/projects" <<<"$project_payload")
project_id=$(jq -er '.id' <<<"$project_response")

upload_snapshot() {
  local artifact="$1"
  local run_name="$2"
  local artifact_filename validation_summary expected_endpoints expected_resources expected_items expected_errors
  local run_payload run_response run_id upload_response status started_at

  artifact_filename=$(basename "$artifact")
  validation_summary=$("$script_dir/validate-ndjson.py" --summary-only --json "$artifact")
  expected_endpoints=$(jq -er '.endpoints' <<<"$validation_summary")
  expected_resources=$(jq -er '.resources' <<<"$validation_summary")
  expected_items=$(jq -er '.items' <<<"$validation_summary")
  expected_errors=$(jq -er '.errors' <<<"$validation_summary")

  run_payload=$(jq -nc --arg name "$run_name" --arg filename "$artifact_filename" '{
    name: $name,
    description: "Synthetic SharePoint collector contract validation",
    target_scope: {source: "sharepoint-smoke", artifact: $filename}
  }')
  run_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -c "$cookies" \
    -H "content-type: application/json" -H "x-csrf-token: $csrf" \
    --data-binary @- "$api_base/projects/$project_id/runs" <<<"$run_payload")
  run_id=$(jq -er '.id' <<<"$run_response")

  upload_response=$(curl --connect-timeout 5 --max-time "$smoke_timeout_seconds" -fsS \
    -b "$cookies" -c "$cookies" -H "x-csrf-token: $csrf" \
    -H "content-type: application/x-ndjson" -H "x-artifact-filename: $artifact_filename" \
    --data-binary "@$artifact" "$api_base/projects/$project_id/runs/$run_id/artifact")
  jq -e '.ok == true' <<<"$upload_response" >/dev/null

  status=""
  started_at=$(date +%s)
  while (( $(date +%s) - started_at < smoke_timeout_seconds )); do
    run_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
      "$api_base/projects/$project_id/runs/$run_id")
    status=$(jq -r '.status' <<<"$run_response")
    if [[ "$status" == "COMPLETE" || "$status" == "FAILED" ]]; then
      break
    fi
    sleep 1
  done
  if [[ "$status" != "COMPLETE" ]]; then
    echo "FAIL: $run_name ended in status $status" >&2
    jq . <<<"$run_response" >&2
    return 1
  fi

  jq -e \
    --argjson endpoints "$expected_endpoints" \
    --argjson resources "$expected_resources" \
    --argjson items "$expected_items" \
    --argjson errors "$expected_errors" \
    '.summary.endpoints == $endpoints
      and .summary.resources == $resources
      and .summary.items == $items
      and .summary.errors == $errors
      and .collection_context.source == "sharepoint"
      and .collection_context.provider == "sharepoint"
      and .collection_context.collection_mode == "delegated_user_view"
      and .collection_context.auth_mode == "token"
      and .collection_context.auth_type == "delegated"
      and .collection_context.assessed_identity == "synthetic.user@example.test"
      and .collection_context.discovery_completeness == "targeted_scope"
      and .collection_context.materialized_snapshot == true
      and .collection_context.partial == false
      and .collection_context.metadata.files_included == true
      and .collection_context.metadata.content_downloaded == false' \
    <<<"$run_response" >/dev/null
  uploaded_run_id="$run_id"
}

upload_snapshot "$baseline_artifact" "Synthetic SharePoint baseline"
baseline_run_id="$uploaded_run_id"
upload_snapshot "$current_artifact" "Synthetic SharePoint current"
current_run_id="$uploaded_run_id"

diff_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/projects/$project_id/runs/$current_run_id/diff?baseline_run_id=$baseline_run_id")
jq -e '
  .comparison_compatibility.compatible == true
  and (.comparison_compatibility.mismatched_fields | length) == 0
  and .summary.new_shares == 0
  and .summary.disappeared_shares == 0
  and .summary.changed_shares == 1
  and .summary.added_items == 0
  and .summary.removed_items == 1
  and .summary.moved_items == 2
  and (.item_churn | length) == 1
  and .item_churn[0].provider_resource_id == "b!synthetic-drive-id"
  and .item_churn[0].removed_examples == ["/Records/obsolete.txt"]
  and (.item_churn[0].moved_examples
    | map(select(
        .provider_item_id == "synthetic-folder-id"
        and .from_path == "/Records"
        and .to_path == "/Archive"
      ))
    | length) == 1
  and (.item_churn[0].moved_examples
    | map(select(
        .provider_item_id == "synthetic-file-id"
        and .from_path == "/Records/report.docx"
        and .to_path == "/Archive/quarterly-review.docx"
      ))
    | length) == 1
' <<<"$diff_response" >/dev/null

comparison_payload=$(jq -nc \
  --arg baseline_run_id "$baseline_run_id" \
  --arg current_run_id "$current_run_id" \
  '{baseline_run_id: $baseline_run_id, current_run_id: $current_run_id}')
comparison_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" -c "$cookies" \
  -H "content-type: application/json" -H "x-csrf-token: $csrf" \
  --data-binary @- "$api_base/projects/$project_id/comparisons" <<<"$comparison_payload")
comparison_id=$(jq -er '.id' <<<"$comparison_response")
comparison_state=$(jq -r '.state' <<<"$comparison_response")
started_at=$(date +%s)
while [[ "$comparison_state" != "complete" && "$comparison_state" != "failed" ]] \
  && (( $(date +%s) - started_at < smoke_timeout_seconds )); do
  sleep 1
  comparison_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
    "$api_base/projects/$project_id/comparisons/$comparison_id")
  comparison_state=$(jq -r '.state' <<<"$comparison_response")
done
if [[ "$comparison_state" != "complete" ]]; then
  echo "FAIL: materialized comparison ended in state $comparison_state" >&2
  jq . <<<"$comparison_response" >&2
  exit 1
fi
jq -e '
  .summary.exact == false
  and .summary.item_churn_computed == false
  and .summary.total >= 1
  and .progress.phase == "complete"
' <<<"$comparison_response" >/dev/null
comparison_changes=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/projects/$project_id/comparisons/$comparison_id/resource-changes?limit=100")
jq -e '
  (.items | length) >= 1
  and (.items | map(select(
    .provider_resource_id == "b!synthetic-drive-id"
    and .change_type == "changed"
    and (.change_categories | index("item_count")) != null
    and .item_changes.state == "not_computed"
  )) | length) == 1
' <<<"$comparison_changes" >/dev/null

resources_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/projects/$project_id/inventory/resources?provider=sharepoint&resource_type=sharepoint_library&exposure=USER_VISIBLE&run_ids=$current_run_id")
jq -e '(.items | length) == 1
  and .items[0].provider_resource_id == "b!synthetic-drive-id"
  and .items[0].access_level == "list_only"
  and .items[0].web_url == "https://contoso.sharepoint.com/sites/Records/Shared%20Documents"
  and .items[0].exposure_evidence.basis == "graph_delegated_read_context"
  and .items[0].metadata.site_id == "contoso.sharepoint.com,synthetic-site,synthetic-web"
  and .items[0].metadata.drive_id == "b!synthetic-drive-id"
  and .items[0].metadata.access_observation == "graph_item_metadata_enumeration"
  and .items[0].metadata.enumeration_status == "complete"
  and .items[0].metadata.content_state == "populated"
  and .items[0].metadata.file_count == 1
  and .items[0].metadata.folder_count == 1
  and .items[0].metadata.archived_file_count == 1
  and .items[0].metadata.unknown_file_archive_count == 0' \
  <<<"$resources_response" >/dev/null

items_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/projects/$project_id/inventory/items?provider=sharepoint&exposure=USER_VISIBLE&run_ids=$current_run_id")
jq -e '(.items | length) == 2
  and (.items | map(select(
      .provider_item_id == "synthetic-file-id"
      and .provider_parent_id == "synthetic-folder-id"
      and .path == "/Archive/quarterly-review.docx"
      and .size_bytes == 5120
      and .deleted == false
      and .exposure_evidence.basis == "graph_delegated_read_context"
      and .metadata.drive_id == "b!synthetic-drive-id"
      and .metadata.etag == "file-etag-2"
      and .metadata.file_archive_status == "fully_archived"
    )) | length) == 1' <<<"$items_response" >/dev/null

endpoints_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/projects/$project_id/inventory/endpoints?provider=sharepoint&run_ids=$current_run_id")
jq -e '(.items | length) == 1
  and .items[0].endpoint_key == "sharepoint:contoso.sharepoint.com,synthetic-site,synthetic-web"
  and .items[0].metadata.site_id == "contoso.sharepoint.com,synthetic-site,synthetic-web"
  and .items[0].metadata.web_url == "https://contoso.sharepoint.com/sites/Records"
  and .items[0].metadata.assessed_identity == "synthetic.user@example.test"
  and .items[0].metadata.existence_status == "confirmed"
  and .items[0].metadata.lifecycle_state == "available"
  and .items[0].metadata.archive_status == "not_archived"
  and .items[0].metadata.evidence.archive_status_checked == true
  and .items[0].metadata.evidence.archive_status_authoritative == false' \
  <<<"$endpoints_response" >/dev/null

detail_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  "$api_base/settings/projects/$project_id")
jq -e '.run_count == 2 and .artifact_count == 2 and .run_status_counts.COMPLETE == 2' \
  <<<"$detail_response" >/dev/null

delete_payload=$(jq -nc --arg confirm_name "$project_name" '{confirm_name: $confirm_name}')
delete_response=$(curl "${curl_common_args[@]}" -fsS -b "$cookies" \
  -H "content-type: application/json" -H "x-csrf-token: $csrf" \
  -X DELETE --data-binary @- "$api_base/settings/projects/$project_id" <<<"$delete_payload")
jq -e '.ok == true
  and .deleted_run_count == 2
  and .deleted_artifact_count == 2
  and (.artifact_delete_failures | length) == 0' <<<"$delete_response" >/dev/null
project_id=""

echo "SharePoint ingest smoke passed: baseline=$baseline_run_id current=$current_run_id moves=2 removals=1"
