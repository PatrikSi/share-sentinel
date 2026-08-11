#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
template="$root_dir/.env.example"
output="$root_dir/.env"
mode="development"
app_host="localhost"
admin_email="${ADMIN_EMAIL:-admin@example.com}"
admin_password="${ADMIN_PASSWORD:-}"
image_tag="latest"
force="false"

usage() {
  cat <<'EOF'
Usage: ./bootstrap.sh [options]

Generate a complete Share Sentinel .env file with fresh random secrets and
validate that Docker Compose can render the resulting stack.

Options:
  --development            Configure local source builds (default).
  --production HOSTNAME    Configure published images for an HTTPS deployment.
  --admin-email EMAIL      Seed administrator email (default: admin@example.com).
  --image-tag TAG          Published image tag (default: latest).
  --output PATH            Output file (default: .env in the repository root).
  --force                  Replace an existing output file.
  -h, --help               Show this help.

Environment overrides:
  ADMIN_EMAIL              Seed administrator email.
  ADMIN_PASSWORD           Seed administrator password; random when unset.
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
if [[ "$output" != /* ]]; then
  output="$PWD/$output"
fi
if [[ "$app_host" == *://* || "$app_host" == */* || ! "$app_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "FAIL: hostname must contain only letters, numbers, dots, and hyphens" >&2
  exit 2
fi
if [[ ! "$admin_email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
  echo "FAIL: administrator email must be a valid-looking email address" >&2
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

if [[ -z "$admin_password" ]]; then
  admin_password="Ss1-$(random_hex 18)"
fi
if (( ${#admin_password} < 12 )) ||
  [[ ! "$admin_password" =~ [[:lower:]] ]] ||
  [[ ! "$admin_password" =~ [[:upper:]] ]] ||
  [[ ! "$admin_password" =~ [[:digit:]] ]]; then
  echo "FAIL: ADMIN_PASSWORD must be at least 12 characters and include lowercase, uppercase, and a number" >&2
  exit 2
fi
if [[ ! "$admin_password" =~ ^[-A-Za-z0-9._!@%+=:,/]+$ ]]; then
  echo "FAIL: ADMIN_PASSWORD contains characters that are unsafe in an unquoted .env value" >&2
  exit 2
fi

postgres_password=$(random_hex 24)
jwt_secret=$(random_hex 32)
token_pepper=$(random_hex 32)
stack_suffix=$(printf '%s' "$app_host" | tr '[:upper:].' '[:lower:]-' | tr -cd 'a-z0-9-')

if [[ "$mode" == "production" ]]; then
  compose_files="docker-compose.yml"
  stack_name="share-sentinel-$stack_suffix"
  gateway_port="8080"
  cors_origins="https://$app_host"
  trusted_hosts="$app_host"
  secure_cookies="true"
  start_args="up -d"
else
  compose_files="docker-compose.yml:docker-compose.dev.yml"
  stack_name="share-sentinel-dev"
  gateway_port="80"
  cors_origins="http://localhost"
  trusted_hosts="localhost,127.0.0.1"
  secure_cookies="false"
  start_args="up -d --build"
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

compose_validated="false"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  (cd "$root_dir" && docker compose --env-file "$temp_file" config --quiet)
  compose_validated="true"
fi

mv "$temp_file" "$output"
trap - EXIT

if [[ "$compose_validated" == "true" ]]; then
  validation_status="validated"
else
  validation_status="not validated; Docker Compose was not available"
fi

if [[ "$output" == "$root_dir/.env" ]]; then
  start_command="docker compose $start_args"
else
  printf -v quoted_output '%q' "$output"
  start_command="docker compose --env-file $quoted_output $start_args"
fi

cat <<EOF
Created $output for $mode mode.
Compose configuration: $validation_status

Admin login:
  Email:    $admin_email
  Password: $admin_password

Start Share Sentinel:
  cd $root_dir
  $start_command

Store the password securely, sign in once, and rotate it from Settings.
EOF
