from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "render_volumes", Path(__file__).parents[1] / "deploy/k3s/render_volumes.py"
)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

LAYOUT = """\
[global]
share_root = /srv/example/share

[folder Documents]
source = /data/docs

[folder Photos]
source = /data/photos
target = Media/Photos
"""


def layout(tmp_path, text=LAYOUT):
    path = tmp_path / "folders.ini"
    path.write_text(text, encoding="utf-8")
    return path


def test_each_folder_gets_its_own_volume_at_its_target(tmp_path):
    volumes = mod.volume_set(layout(tmp_path))
    by_name = {v["name"]: v for v in volumes}
    assert by_name["share"]["mount"] == "/srv"
    assert by_name["folder-1"]["host"] == "/data/docs" and by_name["folder-1"]["mount"] == "/srv/Documents"
    assert by_name["folder-2"]["host"] == "/data/photos" and by_name["folder-2"]["mount"] == "/srv/Media/Photos"


def test_the_config_volume_is_read_only_and_the_database_is_not(tmp_path):
    by_name = {v["name"]: v for v in mod.volume_set(layout(tmp_path))}
    assert by_name["config"]["read_only"] is True
    assert by_name["database"]["read_only"] is False


def test_environment_overrides_the_database_and_config_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOWBRIDGE_FILES_DB_DIR", "/x/db")
    monkeypatch.setenv("SNOWBRIDGE_FILES_CONFIG_DIR", "/x/cfg")
    by_name = {v["name"]: v for v in mod.volume_set(layout(tmp_path))}
    assert by_name["database"]["host"] == "/x/db" and by_name["config"]["host"] == "/x/cfg"


@pytest.mark.parametrize(
    "text, message",
    [
        ("[folder A]\nsource = /a\n", "share_root"),
        ("[global]\nshare_root = /s\n[folder A]\nsource = relative\n", "absolute"),
        ("[global]\nshare_root = /s\n[folder A]\nsource = /a\ntarget = ../out\n", "inside the share root"),
        ("[global]\nshare_root = /s\n[folder A]\nsource = /a\ntarget = /abs\n", "inside the share root"),
        ("[global]\nshare_root = /s\n[folder A]\nsource = /a\n[folder B]\nsource = /b\ntarget = A\n", "share one target"),
        ('[global]\nshare_root = /s\n[folder A]\nsource = /a"b\n', "unsafe"),
    ],
)
def test_an_unsafe_or_incomplete_layout_is_refused(tmp_path, text, message):
    with pytest.raises(mod.LayoutError, match=message):
        mod.volume_set(layout(tmp_path, text))


def test_a_missing_layout_file_is_an_error(tmp_path):
    with pytest.raises(mod.LayoutError, match="cannot read"):
        mod.volume_set(tmp_path / "nope.ini")


def test_volumes_render_a_local_pv_pinned_to_the_node(tmp_path):
    text = mod.render_volumes("node-a", mod.volume_set(layout(tmp_path)))
    assert text.count("kind: PersistentVolume\n") == 5 and text.count("kind: PersistentVolumeClaim") == 5
    assert 'path: "/data/photos"' in text and 'values: ["node-a"]' in text
    assert "persistentVolumeReclaimPolicy: Retain" in text and "hostPath" not in text


def test_the_workload_gets_every_mount_and_claim(tmp_path):
    template = "      volumeMounts:\n" + mod.MARK_MOUNTS + "\n      volumes:\n" + mod.MARK_VOLUMES + "\n"
    out = mod.render_workload(template, mod.volume_set(layout(tmp_path)))
    assert '{name: folder-2, mountPath: "/srv/Media/Photos"}' in out
    assert '{name: config, mountPath: "/config", readOnly: true}' in out
    assert "claimName: folder-1" in out and "@@" not in out


def test_a_template_without_markers_is_refused(tmp_path):
    with pytest.raises(mod.LayoutError, match="markers"):
        mod.render_workload("nothing here", mod.volume_set(layout(tmp_path)))


def test_the_committed_deployment_template_has_both_markers():
    text = (Path(__file__).parents[1] / "deploy/k3s/manifests/20-deployment.yaml").read_text(encoding="utf-8")
    assert mod.MARK_MOUNTS in text and mod.MARK_VOLUMES in text
