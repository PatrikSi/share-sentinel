#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(cd "$script_dir/.." && pwd)
template="$root_dir/.env.example"
output="$root_dir/.env"
mode="development"
app_host="localhost"
admin_email="admin@example.com"
image_registry="ghcr.io/patriksi"
image_tag="latest"
force="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap-env.sh [options]

Generate a complete Share Sentinel environment file with random secrets.

Options:
  --development            Generate local-development settings (default).
  --production HOSTNAME    Generate production-style settings for HOSTNAME.
  --admin-email EMAIL      Seed administrator email (default: admin@example.com).
  --registry REGISTRY      Container registry namespace (default: ghcr.io/patriksi).
  --image-tag TAG          Application image tag (default: latest).
  --output PATH            Output file (default: .env in the repository root).
  --force                  Replace an existing output file.
  -h, --help               Show this help.
EOF
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    echo "FAIL: $option requires a value" >&2
    usage >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --development)
      mode="development"
      app_host="localhost"
      shift
      ;;
    --production)
      require_value "$1" "${2:-}"
      mode="production"
      app_host="$2"
      shift 2
      ;;
    --admin-email)
      require_value "$1" "${2:-}"
      admin_email="$2"
      shift 2
      ;;
    --registry)
      require_value "$1" "${2:-}"
      image_registry="${2%/}"
      shift 2
      ;;
    --image-tag)
      require_value "$1" "${2:-}"
      image_tag="$2"
      shift 2
      ;;
    --output)
      require_value "$1" "${2:-}"
      output="$2"
      shift 2
      ;;
    --force)
      force="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "FAIL: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$template" ]]; then
  echo "FAIL: environment template not found: $template" >&2
  exit 1
fi
if [[ "$app_host" == *://* || "$app_host" == */* || ! "$app_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "FAIL: hostname must contain only letters, numbers, dots, and hyphens" >&2
  exit 2
fi
if [[ ! "$admin_email" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "FAIL: --admin-email must be a valid-looking email address" >&2
  exit 2
fi
if [[ ! "$image_registry" =~ ^[A-Za-z0-9._:/-]+$ ]]; then
  echo "FAIL: --registry contains unsupported characters" >&2
  exit 2
fi
if [[ ! "$image_tag" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "FAIL: --image-tag contains unsupported characters" >&2
  exit 2
fi
if [[ -e "$output" && "$force" != "true" ]]; then
  echo "FAIL: $output already exists; pass --force to replace it" >&2
  exit 1
fi

random_hex() {
  local bytes="$1"
  od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n'
}

postgres_password=$(random_hex 24)
jwt_secret=$(random_hex 32)
token_pepper=$(random_hex 32)
admin_password="Ss1-$(random_hex 18)"
stack_suffix=$(printf '%s' "$app_host" | tr '[:upper:].' '[:lower:]-' | tr -cd 'a-z0-9-')

if [[ "$mode" == "production" ]]; then
  compose_files="docker-compose.yml"
  stack_name="share-sentinel-$stack_suffix"
  gateway_port="8080"
  cors_origins="https://$app_host"
  trusted_hosts="$app_host"
  secure_cookies="true"
else
  compose_files="docker-compose.yml:docker-compose.dev.yml"
  stack_name="share-sentinel-dev"
  gateway_port="80"
  cors_origins="http://localhost"
  trusted_hosts="localhost,127.0.0.1"
  secure_cookies="false"
fi

output_dir=$(dirname "$output")
mkdir -p "$output_dir"
temp_file=$(mktemp "$output_dir/.share-sentinel-env.XXXXXX")
cleanup() {
  if [[ -e "$temp_file" ]]; then
    rm "$temp_file"
  fi
}
trap cleanup EXIT

awk \
  -v compose_files="$compose_files" \
  -v app_env="$mode" \
  -v app_host="$app_host" \
  -v stack_name="$stack_name" \
  -v image_registry="$image_registry" \
  -v image_tag="$image_tag" \
  -v gateway_port="$gateway_port" \
  -v postgres_password="$postgres_password" \
  -v jwt_secret="$jwt_secret" \
  -v token_pepper="$token_pepper" \
  -v cors_origins="$cors_origins" \
  -v trusted_hosts="$trusted_hosts" \
  -v secure_cookies="$secure_cookies" \
  -v admin_email="$admin_email" \
  -v admin_password="$admin_password" '
BEGIN {
  replacement["COMPOSE_FILE"] = compose_files
  replacement["APP_ENV"] = app_env
  replacement["APP_HOST"] = app_host
  replacement["SHARE_SENTINEL_STACK"] = stack_name
  replacement["SHARE_SENTINEL_IMAGE_REGISTRY"] = image_registry
  replacement["SHARE_SENTINEL_IMAGE_TAG"] = image_tag
  replacement["GATEWAY_BIND_ADDRESS"] = "127.0.0.1"
  replacement["GATEWAY_HTTP_PORT"] = gateway_port
  replacement["POSTGRES_PASSWORD"] = postgres_password
  replacement["JWT_SECRET"] = jwt_secret
  replacement["TOKEN_PEPPER"] = token_pepper
  replacement["CORS_ORIGINS"] = cors_origins
  replacement["TRUSTED_HOSTS"] = trusted_hosts
  replacement["AUTH_COOKIE_SECURE"] = secure_cookies
  replacement["SEED_ADMIN_EMAIL"] = admin_email
  replacement["SEED_ADMIN_PASSWORD"] = admin_password
}
{
  separator = index($0, "=")
  key = separator ? substr($0, 1, separator - 1) : ""
  if (key in replacement) {
    print key "=" replacement[key]
  } else {
    print $0
  }
}
' "$template" > "$temp_file"

chmod 600 "$temp_file"
mv "$temp_file" "$output"
trap - EXIT

echo "Created $output for $mode mode."
echo "Admin email: $admin_email"
echo "Admin password: $admin_password"
echo "Store the password securely, sign in once, and rotate it from Settings."
