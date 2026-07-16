#!/bin/sh
set -eu

api_base=${VITE_API_BASE_URL:-/api}
csrf_cookie_name=${VITE_CSRF_COOKIE_NAME:-share_sentinel_csrf}
csrf_header_name=${VITE_CSRF_HEADER_NAME:-x-csrf-token}

if ! printf '%s' "$api_base" | grep -Eq '^/[A-Za-z0-9._~/-]*$'; then
  echo "ERROR: VITE_API_BASE_URL must be a relative URL path" >&2
  exit 1
fi
if ! printf '%s' "$csrf_cookie_name" | grep -Eq '^[A-Za-z0-9_-]+$'; then
  echo "ERROR: VITE_CSRF_COOKIE_NAME contains unsupported characters" >&2
  exit 1
fi
if ! printf '%s' "$csrf_header_name" | grep -Eq '^[A-Za-z0-9-]+$'; then
  echo "ERROR: VITE_CSRF_HEADER_NAME contains unsupported characters" >&2
  exit 1
fi

target=/usr/share/nginx/html/runtime-config.js
temporary=${target}.tmp
printf 'window.__SHARE_SENTINEL_CONFIG__ = Object.freeze({apiBase:"%s",csrfCookieName:"%s",csrfHeaderName:"%s"});\n' \
  "$api_base" "$csrf_cookie_name" "$csrf_header_name" > "$temporary"
chmod 644 "$temporary"
mv "$temporary" "$target"
