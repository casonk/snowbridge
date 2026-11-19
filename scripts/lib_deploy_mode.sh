# lib_deploy_mode.sh — source this; it only defines functions.
#
# deploy/deploy.sh records which copy of File Browser holds port 8080 in
# ~/.local/state/snowbridge-files/deploy-mode of the user who owns this
# checkout (SNOWBRIDGE_FILES_STATE_DIR replaces that directory, for tests). These scripts run as root, so they look that file up by owner.
# While it says "k3s" the pod serves the port and the host container is
# stopped on purpose; recreating it would fight the forwarder for the port.

# deploy_mode_marker_path REPO_ROOT — print the marker path for the checkout owner.
deploy_mode_marker_path() {
  local repo_root="$1" owner home
  if [[ -n "${SNOWBRIDGE_FILES_STATE_DIR:-}" ]]; then
    printf '%s/deploy-mode\n' "${SNOWBRIDGE_FILES_STATE_DIR}"
    return 0
  fi
  owner="$(stat -c %U "${repo_root}" 2>/dev/null)" || return 1
  home="$(getent passwd "${owner}" | cut -d: -f6)"
  [[ -n "${home}" ]] || return 1
  printf '%s/.local/state/snowbridge-files/deploy-mode\n' "${home}"
}

# k3s_serves_filebrowser REPO_ROOT — true when the marker says the pod holds the port.
k3s_serves_filebrowser() {
  local marker
  marker="$(deploy_mode_marker_path "$1")" || return 1
  [[ -r "${marker}" && "$(<"${marker}")" == "k3s" ]]
}
