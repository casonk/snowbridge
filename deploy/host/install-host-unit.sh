#!/usr/bin/env bash
# install-host-unit.sh — install the user unit that deploy/deploy.sh uses to start and stop the host container.
# No sudo here. It renders the unit with this machine's paths (never committed) and prints the one sudoers rule
# you must install yourself, because the container belongs to root's podman.
#
#   deploy/host/install-host-unit.sh [--print]     --print shows the rendered files and installs nothing
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMPOSE_FILE="${SNOWBRIDGE_FILES_COMPOSE_FILE:-$REPO/config/web/filebrowser/docker-compose.local.yml}"
PODMAN_COMPOSE="$(command -v podman-compose || true)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
[ -f "$COMPOSE_FILE" ] || { echo "compose file not found: $COMPOSE_FILE (set SNOWBRIDGE_FILES_COMPOSE_FILE)" >&2; exit 1; }
[ -n "$PODMAN_COMPOSE" ] || { echo "podman-compose is not installed" >&2; exit 1; }
# Only plain path characters: the values are written into a unit file and a sudoers rule.
both="$COMPOSE_FILE$PODMAN_COMPOSE"
[ -z "${both//[A-Za-z0-9_.\/:+@%-]/}" ] || { echo "paths with spaces, quotes or other special characters are not supported" >&2; exit 1; }

template="$(<"$HERE/snowbridge-filebrowser.service.template")"
unit="${template//@PODMAN_COMPOSE@/$PODMAN_COMPOSE}"
unit="${unit//@COMPOSE_FILE@/$COMPOSE_FILE}"
rule="$USER ALL=(root) NOPASSWD: $PODMAN_COMPOSE -f $COMPOSE_FILE start filebrowser, $PODMAN_COMPOSE -f $COMPOSE_FILE stop filebrowser"

if [ "${1:-}" = "--print" ]; then
  printf '%s\n\n# sudoers rule:\n%s\n' "$unit" "$rule"
  exit 0
fi
install -d "$UNIT_DIR"
printf '%s\n' "$unit" > "$UNIT_DIR/snowbridge-filebrowser.service"
systemctl --user daemon-reload
echo "installed $UNIT_DIR/snowbridge-filebrowser.service (not enabled; deploy.sh enables it for host mode)"
echo
echo "Install this sudoers rule once (it allows exactly these two commands, nothing else):"
echo "  echo '$rule' | sudo tee /etc/sudoers.d/snowbridge-files >/dev/null && sudo chmod 440 /etc/sudoers.d/snowbridge-files && sudo visudo -cf /etc/sudoers.d/snowbridge-files"
