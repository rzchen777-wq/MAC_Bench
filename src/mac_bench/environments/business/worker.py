"""Restore application containers and their data inside a task VM."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from .containers import Engine, create_body
from .services import BusinessServices, service_order


def import_image(images, name, tag):
    """Stream OCI conversion into the host Docker client without a staged archive."""
    command = ['skopeo', 'copy', '--quiet', 'oci:' + str(images) + ':' + name,
               'docker-archive:/dev/stdout:' + tag]
    producer = subprocess.Popen(command, stdout=subprocess.PIPE)
    try:
        with producer.stdout:
            loaded = subprocess.run(['docker', 'load'], stdin=producer.stdout,
                                    stdout=subprocess.DEVNULL)
        converted = producer.wait()
    except BaseException:
        producer.kill()
        producer.wait()
        raise
    if loaded.returncode:
        raise subprocess.CalledProcessError(loaded.returncode, ['docker', 'load'])
    if converted:
        raise subprocess.CalledProcessError(converted, command)


def load_images(root, config):
    missing = []
    for service in config["services"]:
        tag = "mac-bench/" + service["name"] + ":initial"
        if subprocess.run(["docker", "image", "inspect", tag],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            missing.append((service["image"], tag))
    if not missing:
        return
    images = root / 'images'
    index = json.loads((images / 'index.json').read_text())
    descriptors = {d['annotations']['org.opencontainers.image.ref.name']: d for d in index['manifests']}
    def dependencies(descriptor):
        value = descriptor['digest'].removeprefix('sha256:')
        if descriptor['digest'] != 'sha256:' + value or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Invalid OCI digest')
        result = {value}
        if 'manifest' in descriptor['mediaType'] or 'index' in descriptor['mediaType']:
            document = json.loads((images / 'blobs/sha256' / value).read_text())
            for child in [*document.get('manifests', []), *document.get('layers', []),
                          *([document['config']] if 'config' in document else [])]:
                result.update(dependencies(child))
        return result
    needed = {name: dependencies(descriptors[name]) for name, _ in missing}
    # Import large archives while the expanded store is still small.
    missing.sort(key=lambda item: sum((images / 'blobs/sha256' / value).stat().st_size
                                      for value in needed[item[0]]), reverse=True)
    # Free each image's transfer files before importing the next; shared layers
    # remain until the last consumer. No uncompressed docker-archive is staged.
    for position, (name, tag) in enumerate(missing):
        import_image(images, name, tag)
        if not any(other == name for other, _ in missing[position + 1:]):
            del needed[name]
        remaining = set().union(*needed.values()) if needed else set()
        for path in (images / 'blobs/sha256').iterdir():
            if path.name not in remaining:
                path.unlink()


def restore(root, *, start=True):
    root = Path(root)
    config = json.loads((root / "environment.json").read_text())
    load_images(root, config)
    if not start:
        print(json.dumps({"prepared": [s["name"] for s in config["services"]]}))
        return
    engine = Engine(300)
    names = [s["name"] for s in config["services"]]
    ordered = service_order(
        {"containers": names, "dependencies": config["dependencies"]}
    )
    byname = {s["name"]: s for s in config["services"]}
    existing = {
        c["Names"][0].lstrip("/"): c
        for c in engine.call("GET", "/containers/json", query={"all": "true"})
    }
    for name in reversed(ordered):
        if name in existing:
            engine.call(
                "DELETE",
                "/containers/" + existing[name]["Id"],
                query={"force": "true", "v": "false"},
            )
    network_names = {n["Name"] for n in engine.call("GET", "/networks")}
    for name, network in config["networks"].items():
        if name not in network_names:
            engine.call(
                "POST",
                "/networks/create",
                body={
                    "Name": name,
                    "Driver": network["Driver"],
                    "Internal": network.get("Internal", False),
                    "EnableIPv6": network.get("EnableIPv6", False),
                    "IPAM": network["IPAM"],
                    "Options": network.get("Options", {}),
                },
            )
    for name, volume in config["volumes"].items():
        engine.call(
            "POST",
            "/volumes/create",
            body={"Name": name, "Driver": "local", "Labels": {"mac-bench": "task"}},
        )
    for source, mount in config["mounts"].items():
        path = Path(source)
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        subprocess.run(
            [
                "tar",
                "--numeric-owner",
                "--same-owner",
                "--acls",
                "--xattrs",
                "--xattrs-include=*",
                "-xpf",
                str(root / "state" / mount["archive"]),
                "-C",
                str(path.parent),
            ],
            check=True,
        )
    for name in ordered:
        service = byname[name]
        tag = "mac-bench/" + name + ":initial"
        body = create_body(service["configuration"], tag)
        if name == "runtime-hrms":
            # bench's exit hook checks PyPI without a timeout. Startup invokes
            # several bench commands; the native opt-out keeps snapshot loading
            # independent of external update servers, including offline VMs.
            body["Env"] = [value for value in body.get("Env") or []
                           if not value.startswith("FRAPPE_DOCKER_BUILD=")]
            body["Env"].append("FRAPPE_DOCKER_BUILD=1")
        for network_name, network in config["networks"].items():
            if body["HostConfig"].get("NetworkMode") == network["Id"]:
                body["HostConfig"]["NetworkMode"] = network_name
        engine.call("POST", "/containers/create", query={"name": name}, body=body)
    # Preserve the benchmark's existing periodic scheduler policy.
    from mac_bench.environments.packaging.background import _PAUSE, _PRETIX

    if "runtime-hrms" in names:
        subprocess.run(["python3", "-c", _PAUSE], check=True, stdout=subprocess.DEVNULL)
    if "m6-pretix" in names:
        subprocess.run(["python3", "-c", _PRETIX], check=True, stdout=subprocess.DEVNULL)
    from mac_bench.environments.apps.bigcapital.accounting_patch import _INSTALL
    if 'runtime-bigcapital' in names:
        subprocess.run(['python3', '-c', _INSTALL], check=True, stdout=subprocess.DEVNULL)
    services = BusinessServices(engine, names=ordered, dependencies=config['dependencies'],
                                readiness=config['readiness'])
    services.start({name: engine.inspect(name) for name in ordered})
    print(json.dumps({"ready": names}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    restore(args.root, start=not args.prepare_only)
