#!/usr/bin/env bash
# Run rclone against the Snowbridge encrypted config with the KeePassXC-backed
# config-password helper.
#
# A bare `rclone config` would read and write ~/.config/rclone/rclone.conf, the
# tool's own default, and would silently enroll a provider into an UNENCRYPTED
# file outside Snowbridge. Every rclone invocation for this repo goes through
# here so that cannot happen by accident.
#
# Provider enrollment is online and may grant write or deletion permissions.
# Review the consent screen before approving it. This wrapper does not choose a
# scope for you.
#
# Usage:
#   scripts/rclone_snowbridge.sh config          # interactive enrollment
#   scripts/rclone_snowbridge.sh listremotes --long
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config="${SNOWBRIDGE_RCLONE_CONFIG:-${HOME}/.config/snowbridge/rclone.conf}"
helper="${repo_root}/scripts/rclone_config_password.py"

if [ ! -f "${helper}" ]; then
  printf 'error: password helper not found: %s\n' "${helper}" >&2
  exit 2
fi

# auto-pass requires Python 3.11+, and rclone runs the helper with a minimal
# PATH where /usr/bin precedes Homebrew. Resolve a new enough interpreter here
# rather than letting the shebang pick the macOS system Python 3.9.
interpreter=""
for candidate in "${SNOWBRIDGE_PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3; do
  [ -n "${candidate}" ] || continue
  resolved="$(command -v "${candidate}" 2>/dev/null || true)"
  [ -n "${resolved}" ] || continue
  if "${resolved}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    interpreter="${resolved}"
    break
  fi
done
if [ -z "${interpreter}" ]; then
  printf 'error: no Python 3.11+ interpreter found for the auto-pass helper\n' >&2
  exit 2
fi

if [ ! -f "${config}" ]; then
  printf 'note: %s does not exist yet; encrypt it before enrolling a provider\n' "${config}" >&2
fi

exec rclone \
  --config "${config}" \
  --password-command "${interpreter} ${helper}" \
  "$@"
