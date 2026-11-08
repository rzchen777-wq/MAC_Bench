"""Restore application containers and their data inside a task VM."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from .containers import Engine, create_body
from .services import BusinessServices, service_order


def load_images(root, config):
    for service in config["services"]:
        name = service["name"]
        tag = "mac-bench/" + name + ":initial"
        if subprocess.run(
            ["docker", "image", "inspect", tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode:
            archive = root / "image.tar"
            subprocess.run(
                [
                    "skopeo",
                    "copy",
                    "--quiet",
                    "oci:" + str(root / "images") + ":" + service["image"],
                    "docker-archive:" + str(archive) + ":" + tag,
                ],
                check=True,
            )
            subprocess.run(
                ["docker", "load", "-i", str(archive)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            archive.unlink()


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
