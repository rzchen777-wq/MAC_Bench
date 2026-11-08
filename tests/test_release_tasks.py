import hashlib
import json
from pathlib import Path
import pytest
from mac_bench.environments.packaging.bundle import WorldBundle
from mac_bench.environments.business.runtime import selected_images
from mac_bench.runtime.common.hashing import digest


def test_release_directory_is_editable_but_verify_detects_changes(tmp_path):
    (tmp_path / "public").mkdir()
    task = tmp_path / "public/task.txt"
    task.write_text("Original task")
    body = {
        "format": "mac_bench.task.v1",
        "world_id": "sample",
        "public_files": ["public/task.txt"],
        "metadata": {},
        "files": {"public/task.txt": hashlib.sha256(task.read_bytes()).hexdigest()},
    }
    body["revision"] = digest(body)
    (tmp_path / "manifest.json").write_text(json.dumps(body))
    WorldBundle.load(tmp_path, verify=True)
    task.write_text("A locally modified task")
    assert "modified" in WorldBundle.load(tmp_path).public_task()
    with pytest.raises(ValueError, match="changed"):
        WorldBundle.load(tmp_path, verify=True)


def test_oci_selection_only_requires_selected_application(tmp_path):
    (tmp_path / "blobs/sha256").mkdir(parents=True)

    def blob(data, kind):
        raw = json.dumps(data).encode()
        h = hashlib.sha256(raw).hexdigest()
        (tmp_path / "blobs/sha256" / h).write_bytes(raw)
        return {"digest": "sha256:" + h, "size": len(raw), "mediaType": kind}

    layer = blob({"layer": 1}, "application/vnd.oci.image.layer.v1.tar+gzip")
    config = blob({"config": 1}, "application/vnd.oci.image.config.v1+json")
    manifest = blob(
        {"config": config, "layers": [layer]},
        "application/vnd.oci.image.manifest.v1+json",
    )
    manifest["annotations"] = {"org.opencontainers.image.ref.name": "sample-app"}
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    manifest,
                    {
                        "annotations": {"org.opencontainers.image.ref.name": "other"},
                        "digest": "absent",
                    },
                ],
            }
        )
    )
    index, paths = selected_images(tmp_path, {"sample-app"})
    assert len(index["manifests"]) == 1 and len(paths) == 3
    with pytest.raises(ValueError, match="missing"):
        selected_images(tmp_path, {"no-such-app"})


@pytest.mark.parametrize('cross_device', [False, True])
def test_release_blob_export_preserves_bytes_on_same_or_other_filesystem(tmp_path, monkeypatch, cross_device):
    import errno
    import importlib.util
    from mac_bench.runtime.paths import repository_root
    path = repository_root()/'scripts/prepare_dataset.py'
    spec = importlib.util.spec_from_file_location('prepare_dataset_test', path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source, target = tmp_path/'source', tmp_path/'target'
    source.write_bytes(bytes(range(256))*4000)
    if cross_device:
        def fail(*args):raise OSError(errno.EXDEV, 'different filesystem')
        monkeypatch.setattr(module.os, 'link', fail)
    module.link_or_copy(source, target)
    assert target.read_bytes() == source.read_bytes()
    if not cross_device:assert target.stat().st_ino == source.stat().st_ino
