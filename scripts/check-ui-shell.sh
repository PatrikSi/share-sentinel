#!/usr/bin/env bash
set -euo pipefail

base_url="${1:?usage: check-ui-shell.sh BASE_URL [HOST_HEADER]}"
host_header="${2:-}"
base_url="${base_url%/}"
temp_dir=$(mktemp -d)

cleanup() {
  rm -rf "$temp_dir"
}
trap cleanup EXIT

curl_args=(--connect-timeout 3 --max-time 15)
if [[ -n "$host_header" ]]; then
  curl_args+=(-H "Host: $host_header")
fi

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

header_value() {
  local name="$1"
  local file="$2"
  awk -F ': *' -v target="$name" '
    tolower($1) == tolower(target) {
      sub(/\r$/, "", $2)
      print tolower($2)
      exit
    }
  ' "$file"
}

fetch_path() {
  local path="$1"
  local output="$2"
  local headers="$3"
  curl "${curl_args[@]}" -sS -D "$headers" -o "$output" -w '%{http_code}' "$base_url$path"
}

index_body="$temp_dir/index.html"
index_headers="$temp_dir/index.headers"
index_status=$(fetch_path / "$index_body" "$index_headers")
[[ "$index_status" == "200" ]] || fail "UI shell returned HTTP $index_status"
grep -Fq '<div id="root">' "$index_body" || fail "UI shell does not contain the React root"
index_cache=$(header_value cache-control "$index_headers")
[[ "$index_cache" == *no-cache* || "$index_cache" == *no-store* ]] ||
  fail "UI shell is cacheable and can retain stale build asset references"

mapfile -t asset_paths < <(
  grep -Eo '(src|href)="/[^"?]+(\?[^" ]*)?"' "$index_body" |
    sed -E 's/^[^=]+="([^"]+)"$/\1/' |
    sort -u
)

((${#asset_paths[@]} > 0)) || fail "UI shell does not reference any browser assets"
printf '%s\n' "${asset_paths[@]}" | grep -Eq '^/assets/.+\.js$' || fail "UI shell has no built JavaScript entry"
printf '%s\n' "${asset_paths[@]}" | grep -Eq '^/assets/.+\.css$' || fail "UI shell has no built stylesheet"
printf '%s\n' "${asset_paths[@]}" | grep -Fxq '/runtime-config.js' || fail "UI shell has no runtime configuration"
printf '%s\n' "${asset_paths[@]}" | grep -Fxq '/startup-check.js' || fail "UI shell has no startup failure guard"

for path in "${asset_paths[@]}"; do
  safe_name=$(printf '%s' "$path" | tr -c 'A-Za-z0-9._-' '_')
  body="$temp_dir/$safe_name.body"
  headers="$temp_dir/$safe_name.headers"
  status=$(fetch_path "$path" "$body" "$headers")
  [[ "$status" == "200" ]] || fail "UI asset $path returned HTTP $status"
  [[ -s "$body" ]] || fail "UI asset $path is empty"

  content_type=$(header_value content-type "$headers")
  case "$path" in
    *.js)
      [[ "$content_type" == *javascript* ]] || fail "UI asset $path has unexpected content type: ${content_type:-missing}"
      ;;
    *.css)
      [[ "$content_type" == text/css* ]] || fail "UI asset $path has unexpected content type: ${content_type:-missing}"
      ;;
  esac

  cache_control=$(header_value cache-control "$headers")
  if [[ "$path" == /assets/* ]]; then
    [[ "$cache_control" == *max-age=* ]] || fail "hashed UI asset $path is missing a long-lived cache policy"
  else
    [[ "$cache_control" == *no-cache* || "$cache_control" == *no-store* ]] ||
      fail "runtime UI asset $path can become stale"
  fi
done

missing_status=$(fetch_path "/assets/share-sentinel-intentionally-missing.js" "$temp_dir/missing.body" "$temp_dir/missing.headers")
[[ "$missing_status" == "404" ]] ||
  fail "missing UI build assets return HTTP $missing_status instead of 404"

printf 'UI shell and %d browser assets passed startup validation\n' "${#asset_paths[@]}"
