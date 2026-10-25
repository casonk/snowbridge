#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE="${REPO_ROOT}/vendor/filebrowser-upstream"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.local.yml"
MANIFEST="${REPO_ROOT}/config/clockwork/filebrowser-folder-icons.local.toml"
MANIFEST_TEMPLATE="${REPO_ROOT}/config/clockwork/filebrowser-folder-icons.toml.template"
CREDENTIAL_FILE="${HOME}/.config/snowbridge/folder-icons-scheduler.json"
IMAGE="localhost/filebrowser-snowbridge:ai-icons"
ACCESS_CONFIG="${REPO_ROOT}/config/web/filebrowser/access.local.toml"
MODE=""
AUTH_MODE=""
SCHEDULER_USER=""
BASE_URL=""
USERNAME_SET=0
BASE_URL_SET=0
SKIP_PREFETCH=0
MTLS_STAGE_DIR=""

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${MTLS_STAGE_DIR}" && -d "${MTLS_STAGE_DIR}" ]]; then
    rm -rf -- "${MTLS_STAGE_DIR}"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: ./scripts/activate_filebrowser_folder_icons.sh [options]

Build and deploy the prepared File Browser fork, enable the ECU relay, run an
initial prefetch, and install the daily Clockwork user timer.

The web mode defaults to access.local.toml when present. In private-vpn-mtls
mode, the script issues a dedicated client certificate and uses Caddy's mTLS
proxy identity; it needs no File Browser password. In JSON-auth modes, create a
read-only scheduler account with the same Scope/visibility and provide its
password when prompted. Confirm crew-chief has the configured model.

Options:
  --mode private-vpn|private-vpn-mtls|public|public-private-ip
                              Web stack mode. Defaults to access.local.toml, then private-vpn
  --username NAME             JSON-auth scheduler account. Default: folder-icons-scheduler
                              In mTLS mode this must be snowbridge.
  --base-url URL              JSON-auth loopback URL. In mTLS mode the URL is
                              https://files.snowbridge.internal.
  --skip-prefetch              Install the timer without making the initial model call.
  --help                      Show this help text.
EOF
}

while (($#)); do
  case "$1" in
    --mode)
      (($# >= 2)) || fail "--mode requires a value"
      MODE="$2"
      shift 2
      ;;
    --username)
      (($# >= 2)) || fail "--username requires a value"
      SCHEDULER_USER="$2"
      USERNAME_SET=1
      shift 2
      ;;
    --base-url)
      (($# >= 2)) || fail "--base-url requires a value"
      BASE_URL="$2"
      BASE_URL_SET=1
      shift 2
      ;;
    --skip-prefetch)
      SKIP_PREFETCH=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

if [[ -z "${MODE}" ]]; then
  if [[ -f "${ACCESS_CONFIG}" ]]; then
    MODE="$(python3 - "${ACCESS_CONFIG}" <<'PY'
import sys
import tomllib
from pathlib import Path

with Path(sys.argv[1]).open("rb") as stream:
    data = tomllib.load(stream)
print(data.get("runtime", {}).get("mode", "private-vpn"))
PY
)"
  else
    MODE="private-vpn"
  fi
fi

case "${MODE}" in
  private-vpn|private-vpn-mtls|public|public-private-ip) ;;
  *) fail "unsupported web mode: ${MODE}" ;;
esac
[[ "${EUID}" -ne 0 ]] || fail "run this script as your normal login user, without sudo"
if [[ "${MODE}" == "private-vpn-mtls" ]]; then
  AUTH_MODE="mtls-proxy"
  if ((USERNAME_SET)) && [[ "${SCHEDULER_USER}" != "snowbridge" ]]; then
    fail "mTLS proxy authentication maps to the File Browser user 'snowbridge'"
  fi
  SCHEDULER_USER="snowbridge"
  if ((BASE_URL_SET)) && [[ "${BASE_URL}" != "https://files.snowbridge.internal" ]]; then
    fail "mTLS mode uses https://files.snowbridge.internal"
  fi
  BASE_URL="https://files.snowbridge.internal"
else
  AUTH_MODE="json"
  ((USERNAME_SET)) || SCHEDULER_USER="folder-icons-scheduler"
  [[ "${SCHEDULER_USER}" =~ ^[A-Za-z0-9_.-]+$ ]] ||
    fail "scheduler username may contain only letters, numbers, dot, underscore, and hyphen"
  ((BASE_URL_SET)) || BASE_URL="http://127.0.0.1:8080"
fi

if [[ "${AUTH_MODE}" == "mtls-proxy" ]]; then
  printf 'Detected %s; using %s with the Caddy mTLS identity mapped to File Browser user %s.\n' \
    "${MODE}" "${BASE_URL}" "${SCHEDULER_USER}"
else
  printf 'Detected %s; using JSON login at %s as %s.\n' \
    "${MODE}" "${BASE_URL}" "${SCHEDULER_USER}"
fi

[[ -f "${ENV_FILE}" ]] || fail "missing local env file: ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || fail "missing local Compose file: ${COMPOSE_FILE}"
[[ -x "${REPO_ROOT}/scripts/setup_crew_chief_relay.sh" ]] ||
  fail "relay setup script is missing or not executable"
[[ -x "${REPO_ROOT}/scripts/build_filebrowser_fork_image.sh" ]] ||
  fail "fork image build script is missing or not executable"
[[ -x "${REPO_ROOT}/scripts/setup_caddy_filebrowser.sh" ]] ||
  fail "web stack setup script is missing or not executable"
[[ -f "${REPO_ROOT}/scripts/prefetch_filebrowser_folder_icons.py" ]] ||
  fail "prefetch helper is missing"
[[ -x "${REPO_ROOT}/scripts/check_filebrowser_backend.sh" ]] ||
  fail "backend check script is missing or not executable"
if [[ "${AUTH_MODE}" == "mtls-proxy" ]]; then
  [[ -f "${REPO_ROOT}/scripts/export_caddy_mtls_profile.py" ]] ||
    fail "mTLS identity exporter is missing"
  [[ -f "${HOME}/.config/wiring-harness/certs/ca.crt" ]] ||
    fail "Wiring Harness server CA is missing at ${HOME}/.config/wiring-harness/certs/ca.crt"
fi
[[ -f "${WORKSPACE}/filebrowser" ]] ||
  fail "fork binary is missing; build the File Browser fork first"
[[ -f "${WORKSPACE}/frontend/dist/index.html" ]] ||
  fail "fork frontend bundle is missing; build the File Browser frontend first"
[[ "$(git -C "${WORKSPACE}" branch --show-current)" == "snowbridge/ai-folder-icons" ]] ||
  fail "vendor/filebrowser-upstream must be on branch snowbridge/ai-folder-icons"
if [[ ! -e "${MANIFEST}" ]]; then
  python3 - "${MANIFEST_TEMPLATE}" "${MANIFEST}" "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path

template_path, manifest_path, repo_root = map(Path, sys.argv[1:])
placeholder = "%h/git/util-repos/snowbridge"
text = template_path.read_text(encoding="utf-8")
if placeholder not in text:
    raise SystemExit(f"manifest template does not contain {placeholder}")
with manifest_path.open("x", encoding="utf-8") as stream:
    stream.write(text.replace(placeholder, str(repo_root)))
PY
fi
[[ -f "${MANIFEST}" ]] || fail "local Clockwork manifest is missing: ${MANIFEST}"
command -v sudo >/dev/null 2>&1 || fail "sudo is required for host deployment"
command -v clockwork >/dev/null 2>&1 || fail "clockwork must be installed and available in PATH"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is required for the user timer"
systemctl --user show-environment >/dev/null 2>&1 ||
  fail "the systemd user manager is not available"

if [[ "${AUTH_MODE}" == "mtls-proxy" ]]; then
  if [[ -e "${CREDENTIAL_FILE}" ]]; then
    [[ ! -L "${CREDENTIAL_FILE}" && -f "${CREDENTIAL_FILE}" ]] ||
      fail "scheduler credential path must be a regular file, not a symlink"
    stored_identity="$(python3 - "${CREDENTIAL_FILE}" <<'PY'
import json
import os
import stat
import sys

path = sys.argv[1]
metadata = os.stat(path, follow_symlinks=False)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
    raise SystemExit("credential file must be owned by this user with mode 0600")
with open(path, encoding="utf-8") as stream:
    config = json.load(stream)
if not isinstance(config, dict):
    raise SystemExit("credential file must be a JSON object")
print(f"{config.get('auth_method', 'json')}\t{config.get('username', '')}")
PY
)" || fail "cannot read existing scheduler credentials safely"
    IFS=$'\t' read -r stored_auth stored_user <<<"${stored_identity}"
    [[ "${stored_auth}" == "mtls-proxy" && "${stored_user}" == "snowbridge" ]] ||
      fail "existing credential file is not for the Snowbridge mTLS proxy identity; move it aside first"
  fi
  read -r -p "Confirm mTLS is active, the proxy user 'snowbridge' has the share scope, and crew-chief has the configured model [y/N]: " ready </dev/tty ||
    fail "an interactive terminal is required"
  [[ "${ready}" =~ ^[Yy]$ ]] ||
    fail "verify the mTLS proxy identity and model, then rerun"
else
  read -r -p "Confirm the read-only scheduler account has the same Scope/visibility and crew-chief has the configured model [y/N]: " ready </dev/tty ||
    fail "an interactive terminal is required"
  [[ "${ready}" =~ ^[Yy]$ ]] ||
    fail "create the scheduler account and verify crew-chief/model readiness, then rerun"

  if [[ -e "${CREDENTIAL_FILE}" ]]; then
    [[ ! -L "${CREDENTIAL_FILE}" && -f "${CREDENTIAL_FILE}" ]] ||
      fail "scheduler credential path must be a regular file, not a symlink"
    stored_identity="$(python3 - "${CREDENTIAL_FILE}" <<'PY'
import json
import os
import stat
import sys

path = sys.argv[1]
metadata = os.stat(path, follow_symlinks=False)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
    raise SystemExit("credential file must be owned by this user with mode 0600")
with open(path, encoding="utf-8") as stream:
    config = json.load(stream)
if not isinstance(config, dict):
    raise SystemExit("credential file must be a JSON object")
print(f"{config.get('auth_method', 'json')}\t{config.get('username', '')}")
PY
)" || fail "cannot read existing scheduler credentials safely"
    IFS=$'\t' read -r stored_auth stored_user <<<"${stored_identity}"
    [[ "${stored_auth}" == "json" ]] ||
      fail "existing credential file uses ${stored_auth}; move it aside before switching to JSON auth"
    [[ "${stored_user}" == "${SCHEDULER_USER}" ]] ||
      fail "existing credential file is for '${stored_user}', but --username is '${SCHEDULER_USER}'"
    printf 'Using existing owner-only credentials for %s.\n' "${SCHEDULER_USER}"
  else
    read -r -p "Loopback File Browser URL [${BASE_URL}]: " entered_url </dev/tty ||
      fail "an interactive terminal is required to configure scheduler credentials"
    [[ -z "${entered_url}" ]] || BASE_URL="${entered_url}"
    python3 - "${BASE_URL}" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
parsed = urlsplit(value)
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit("base URL must be an http(s) loopback URL")
try:
    parsed.port
except ValueError as exc:
    raise SystemExit("base URL has an invalid port") from exc
if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
    raise SystemExit("base URL must not include credentials, query, fragment, or path prefix")
hostname = parsed.hostname.lower()
try:
    is_loopback = ipaddress.ip_address(hostname).is_loopback
except ValueError:
    is_loopback = hostname == "localhost"
if not is_loopback:
    raise SystemExit("base URL must point to localhost or a loopback IP address")
PY
    read -r -s -p "Password for ${SCHEDULER_USER}: " scheduler_password </dev/tty ||
      fail "an interactive terminal is required to read the scheduler password"
    printf '\n' >/dev/tty
    [[ -n "${scheduler_password}" ]] || fail "scheduler password must not be empty"
    install -d -m 0700 "$(dirname "${CREDENTIAL_FILE}")"
    chmod 0700 "$(dirname "${CREDENTIAL_FILE}")"
    printf '%s' "${scheduler_password}" | python3 -c '
import json
import os
import sys
import tempfile

path, base_url, username = sys.argv[1:]
password = sys.stdin.read()
if not password:
    raise SystemExit("scheduler password must not be empty")
payload = json.dumps({"base_url": base_url, "username": username, "password": password}) + "\n"
parent = os.path.dirname(path)
fd, temporary = tempfile.mkstemp(prefix=".folder-icons-scheduler.", dir=parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
' "${CREDENTIAL_FILE}" "${BASE_URL}" "${SCHEDULER_USER}"
    unset scheduler_password
  fi
fi

sudo -v

# Update only the three feature settings, preserving the protected env file's
# owner, mode, comments, and all unrelated host values.
sudo python3 - "${ENV_FILE}" "${SCHEDULER_USER}" "${IMAGE}" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
username, image = sys.argv[2:]
if path.is_symlink() or not path.is_file():
    raise SystemExit("local env file must be a regular file, not a symlink")
metadata = path.stat()
updates = {
    "FILEBROWSER_AI_FOLDER_ICONS": "true",
    "FILEBROWSER_AI_FOLDER_ICONS_PREFETCH_USER": username,
    "FILEBROWSER_IMAGE": image,
}
text = path.read_text(encoding="utf-8")
lines = text.splitlines()
seen = set()
result = []
for line in lines:
    match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
    key = match.group(1) if match else None
    if key not in updates:
        result.append(line)
    elif key not in seen:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")
payload = "\n".join(result) + "\n"
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchown(fd, metadata.st_uid, metadata.st_gid)
    os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY

printf '\n[1/7] Installing the crew-chief socket relay...\n'
sudo "${REPO_ROOT}/scripts/setup_crew_chief_relay.sh" --env-file "${ENV_FILE}"

printf '\n[2/7] Building the File Browser image from the prepared fork...\n'
sudo "${REPO_ROOT}/scripts/build_filebrowser_fork_image.sh" \
  --skip-binary-build --image "${IMAGE}"

printf '\n[3/7] Recreating the web stack...\n'
sudo "${REPO_ROOT}/scripts/setup_caddy_filebrowser.sh" \
  --mode "${MODE}" --env-file "${ENV_FILE}" --compose-file "${COMPOSE_FILE}" --recreate

printf '\n[4/7] Checking the File Browser backend...\n'
sudo "${REPO_ROOT}/scripts/check_filebrowser_backend.sh" \
  --env-file "${ENV_FILE}" --compose-file "${COMPOSE_FILE}" \
  --wait-attempts 10 --wait-sleep 3

if [[ "${AUTH_MODE}" == "mtls-proxy" ]]; then
  printf '\n[5/7] Preparing a dedicated mTLS client identity...\n'
  install -d -m 0700 "$(dirname "${CREDENTIAL_FILE}")"
  chmod 0700 "$(dirname "${CREDENTIAL_FILE}")"
  MTLS_STAGE_DIR="$(mktemp -d "$(dirname "${CREDENTIAL_FILE}")/.folder-icons-mtls.XXXXXX")"
  chmod 0700 "${MTLS_STAGE_DIR}"

  CADDY_DATA_DIR="$(sudo python3 - "${ENV_FILE}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = "/var/lib/snowbridge/caddy/data"
for line in path.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^\s*CADDY_DATA_DIR\s*=\s*(.*?)\s*$", line)
    if match:
        value = match.group(1).strip("\"'")
if not value.startswith("/"):
    raise SystemExit("CADDY_DATA_DIR must be an absolute path")
print(value)
PY
)"
  CLIENT_CA_CERT="${CADDY_DATA_DIR}/mtls/client-ca.crt"
  CLIENT_CA_KEY="${CADDY_DATA_DIR}/mtls/client-ca.key"
  SERVER_CA_CERT="${HOME}/.config/wiring-harness/certs/ca.crt"
  SERVER_CA_COPY="${HOME}/.config/snowbridge/wiring-harness-server-ca.crt"
  ISSUED_DIR="${CADDY_DATA_DIR}/mtls/issued"
  sudo python3 "${REPO_ROOT}/scripts/export_caddy_mtls_profile.py" \
    --device-name folder-icons-scheduler \
    --server-root-cert "${SERVER_CA_CERT}" \
    --client-ca-cert "${CLIENT_CA_CERT}" \
    --client-ca-key "${CLIENT_CA_KEY}" \
    --issued-dir "${ISSUED_DIR}" \
    --output "${MTLS_STAGE_DIR}/scheduler.mobileconfig" \
    --p12-output "${MTLS_STAGE_DIR}/scheduler.p12" \
    --owner "$(id -un)" --group "$(id -gn)" >/dev/null
  sudo install -m 0644 -o "$(id -u)" -g "$(id -g)" \
    "${ISSUED_DIR}/snowbridge-caddy-mtls-folder-icons-scheduler.crt" \
    "${HOME}/.config/snowbridge/folder-icons-scheduler.crt"
  sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" \
    "${ISSUED_DIR}/snowbridge-caddy-mtls-folder-icons-scheduler.key" \
    "${HOME}/.config/snowbridge/folder-icons-scheduler.key"
  sudo install -m 0644 -o "$(id -u)" -g "$(id -g)" \
    "${SERVER_CA_CERT}" "${SERVER_CA_COPY}"
  python3 - "${CREDENTIAL_FILE}" "${BASE_URL}" \
    "${HOME}/.config/snowbridge/folder-icons-scheduler.crt" \
    "${HOME}/.config/snowbridge/folder-icons-scheduler.key" \
    "${SERVER_CA_COPY}" <<'PY'
import json
import os
import sys
import tempfile

path, base_url, client_cert, client_key, server_ca = sys.argv[1:]
payload = json.dumps({
    "auth_method": "mtls-proxy",
    "base_url": base_url,
    "username": "snowbridge",
    "client_cert_path": client_cert,
    "client_key_path": client_key,
    "server_ca_path": server_ca,
}) + "\n"
parent = os.path.dirname(path)
fd, temporary = tempfile.mkstemp(prefix=".folder-icons-scheduler.", dir=parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
else
  printf '\n[5/7] JSON account credentials are ready.\n'
fi

if ((SKIP_PREFETCH)); then
  printf '\n[6/7] Skipping the initial prefetch as requested.\n'
else
  printf '\n[6/7] Running the initial prefetch (up to 40 folders)...\n'
  python3 "${REPO_ROOT}/scripts/prefetch_filebrowser_folder_icons.py"
fi

printf '\n[7/7] Installing and enabling the daily Clockwork timer...\n'
clockwork install --manifest "${MANIFEST}" --target systemd-user
systemctl --user enable --now snowbridge-folder-icon-prefetch.timer
systemctl --user is-active --quiet snowbridge-folder-icon-prefetch.timer ||
  fail "Clockwork timer did not become active"

printf '\nFolder icon prefetch is deployed and scheduled.\n'
