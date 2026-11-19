#!/usr/bin/env python3
"""Render the pod's volumes from the machine's share layout. Standard library only.

The pod must mount each real source folder by itself. A local volume is a
non-recursive bind mount, so the bind aliases under the share root that Samba
uses would arrive empty (and a watchdog remount would never reach the pod).

Usage:
  render_volumes.py volumes   NODE   > volumes.yaml       PersistentVolumes and Claims
  render_volumes.py workload  NODE   < 20-deployment.yaml > deployment.yaml
  render_volumes.py paths                                  one host path per line (for labelling)
  render_volumes.py mounts                                 the share folder mount paths in the pod, one per line

Machine-specific values come from the gitignored share layout and from
environment variables; nothing is committed:
  SNOWBRIDGE_FILES_LAYOUT      folders.local.ini   (default: config/share-layout/folders.local.ini)
  SNOWBRIDGE_FILES_DB_DIR      File Browser database directory
  SNOWBRIDGE_FILES_CONFIG_DIR  File Browser config directory (settings.json, branding/)
"""

from __future__ import annotations

import configparser
import os
import re
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
NAMESPACE = "snowbridge-files"
STORAGE_CLASS = "local-host"
DEFAULT_LAYOUT = REPO / "config/share-layout/folders.local.ini"
DEFAULT_DB_DIR = "/var/lib/snowbridge/filebrowser/database"
DEFAULT_CONFIG_DIR = "/var/lib/snowbridge/filebrowser/config"
SHARE_MOUNT = "/srv"


class LayoutError(Exception):
    """The share layout cannot be turned into volumes; the message is safe to show."""


def read_layout(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """Return (share_root, [(source, target relative to the share root), ...])."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except OSError as exc:
        raise LayoutError(f"cannot read the share layout {path}: {exc}") from exc
    if not parser.has_option("global", "share_root"):
        raise LayoutError(f"{path} has no [global] share_root")
    share_root = parser["global"]["share_root"].strip()
    folders: list[tuple[str, str]] = []
    for section in parser.sections():
        if not section.startswith("folder "):
            continue
        source = parser[section].get("source", "").strip()
        target = parser[section].get("target", section[len("folder ") :]).strip()
        if not source.startswith("/"):
            raise LayoutError(f"[{section}] source must be an absolute path")
        clean = PurePosixPath(target)
        if clean.is_absolute() or ".." in clean.parts or not clean.parts:
            raise LayoutError(f"[{section}] target must be a relative path inside the share root")
        folders.append((source, str(clean)))
    targets = [target for _, target in folders]
    if len(set(targets)) != len(targets):
        raise LayoutError("two folders share one target")
    return share_root, folders


def volume_set(layout: Path | None = None) -> list[dict]:
    """Every host directory the pod mounts: name, host path, container path, read-only."""
    layout = layout or Path(os.environ.get("SNOWBRIDGE_FILES_LAYOUT", DEFAULT_LAYOUT))
    share_root, folders = read_layout(layout)
    volumes = [
        {"name": "share", "host": share_root, "mount": SHARE_MOUNT, "read_only": False},
        {"name": "database", "host": os.environ.get("SNOWBRIDGE_FILES_DB_DIR", DEFAULT_DB_DIR),
         "mount": "/database", "read_only": False},
        {"name": "config", "host": os.environ.get("SNOWBRIDGE_FILES_CONFIG_DIR", DEFAULT_CONFIG_DIR),
         "mount": "/config", "read_only": True},
    ]
    for index, (source, target) in enumerate(folders, start=1):
        volumes.append(
            {"name": f"folder-{index}", "host": source, "mount": f"{SHARE_MOUNT}/{target}", "read_only": False}
        )
    for volume in volumes:
        if re.search(r"[\n\r\"']", volume["host"]):
            raise LayoutError(f"unsafe characters in a host path for {volume['name']}")
    return volumes


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_volumes(node: str, volumes: list[dict]) -> str:
    docs = []
    for volume in volumes:
        pv = f"snowbridge-files-{volume['name']}"
        docs.append(
            f"""apiVersion: v1
kind: PersistentVolume
metadata:
  name: {pv}
spec:
  capacity: {{storage: 1Gi}}
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: {STORAGE_CLASS}
  local:
    path: {_quote(volume['host'])}
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - {{key: kubernetes.io/hostname, operator: In, values: [{_quote(node)}]}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {volume['name']}
  namespace: {NAMESPACE}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: {STORAGE_CLASS}
  volumeName: {pv}
  resources: {{requests: {{storage: 1Gi}}}}
"""
        )
    return "---\n".join(docs)


MARK_MOUNTS = "# @@VOLUME_MOUNTS@@"
MARK_VOLUMES = "# @@VOLUMES@@"


def render_workload(template: str, volumes: list[dict]) -> str:
    if MARK_MOUNTS not in template or MARK_VOLUMES not in template:
        raise LayoutError("the deployment template lost its volume markers")
    mounts = []
    claims = []
    for volume in volumes:
        read_only = ", readOnly: true" if volume["read_only"] else ""
        mounts.append(f"            - {{name: {volume['name']}, mountPath: {_quote(volume['mount'])}{read_only}}}")
        claims.append(
            f"        - name: {volume['name']}\n          persistentVolumeClaim: {{claimName: {volume['name']}}}"
        )
    return template.replace(MARK_MOUNTS, "\n".join(mounts)).replace(MARK_VOLUMES, "\n".join(claims))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"volumes", "workload", "paths", "mounts"}:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        volumes = volume_set()
        if argv[0] == "paths":
            print("\n".join(volume["host"] for volume in volumes))
        elif argv[0] == "mounts":
            print("\n".join(v["mount"] for v in volumes if v["name"].startswith("folder-")))
        elif argv[0] == "volumes":
            if len(argv) < 2:
                print("usage: render_volumes.py volumes NODE", file=sys.stderr)
                return 2
            sys.stdout.write(render_volumes(argv[1], volumes))
        else:
            sys.stdout.write(render_workload(sys.stdin.read(), volumes))
    except LayoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
