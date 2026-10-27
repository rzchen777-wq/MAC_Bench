import hashlib
import json
from pathlib import Path
import pytest
from mac_bench.download import download_file, download_tasks
from mac_bench.data_cli import export_task
from mac_bench.environments.packaging.bundle import seal_bundle


def test_download_checks_bytes_and_reuses_existing_file(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"dataset")
    target = tmp_path / "downloaded"
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    download_file(source.as_uri(), target, checksum)
    assert target.read_bytes() == b"dataset"
    source.unlink()
    download_file(source.as_uri(), target, checksum)
    assert not target.with_name("downloaded.part").exists()


def test_failed_checksum_does_not_replace_existing_file(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"broken")
    target = tmp_path / "downloaded"
    target.write_bytes(b"previous")
    with pytest.raises(ValueError, match="checksum"):
        download_file(source.as_uri(), target, "0" * 64)
    assert target.read_bytes() == b"previous"


def test_download_installs_task_and_shared_assets(tmp_path):
    source = tmp_path / "task"
    (source / "public").mkdir(parents=True)
    (source / "public/task.txt").write_text("Task")
    world = seal_bundle(
        source,
        world_id="sample",
        public_files=["public/task.txt"],
        metadata={"world_family": "developer_release"},
    )
    row = {
        "task_id": "sample",
        "world_revision": world.manifest["revision"],
        "category": "developer_release",
        "split": "dev",
        "bundle": "tasks/sample",
    }
    delivery = tmp_path / "release"
    delivery.mkdir()
    entry = export_task(source, delivery / "sample.tar.gz", row)
    asset = delivery / "example"
    asset.write_bytes(b"environment")
    entry["environment_assets"] = ["environments/example"]
    index = delivery / "downloads.json"
    index.write_text(
        json.dumps(
            {
                "tasks": [entry],
                "assets": {
                    "environments/example": {
                        "url": "example",
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    }
                },
            }
        )
    )
    installed = tmp_path / "fresh"
    download_tasks(installed, index.as_uri(), [row])
    assert (installed / "data/tasks/sample/public/task.txt").read_text() == "Task"
    assert (installed / "data/environments/example").read_bytes() == b"environment"
