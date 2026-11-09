"""Inspect task environments, check environment loading, and export Docker build materials."""

import argparse
from dataclasses import asdict
from mac_bench.runtime.sandbox.providers import provider_name, create_backend, resolve_config
from mac_bench.environments.engineering.public_workspace import initialization_commands

import json
import shutil
from pathlib import Path
from uuid import uuid4

from mac_bench.data_cli import catalog_rows
from mac_bench.runtime.paths import repository_root, artifact_root
from mac_bench.evaluation.tasks.catalog import open_task, task_record
from mac_bench.evaluation.tasks.protocol import ENGINEERING
from mac_bench.environments.engineering.preparation import (
    read_environment,
    dependency_recipe,
)


def describe(bundle, category):
    from mac_bench.environments.packaging.bundle import WorldBundle

    world = WorldBundle.load(bundle)
    metadata = world.manifest["metadata"]
    if category in ENGINEERING:
        environment = read_environment(world.root)

        return {
            "family": "software",
            "provider": provider_name("software"),
            "base_image": environment["image"],
            "base_key": dependency_recipe(environment),
            "task_resources": [
                "workspace archive",
                "public materials",
                "task initialization",
            ],
            "loading": [
                "prepare dependency image",
                "create task sandbox",
                "load starter and materials",
                "run task initialization",
            ],
            "cleanup": "terminate task and grader sandboxes; retain dependency image and run results",
        }
    return {
        "family": "business",
        "provider": provider_name("business"),
        "applications": metadata.get("apps", []),
        "task_resources": [
            "initial application state",
            "public materials",
            "tool interface",
            "outcome verifier",
        ],
        "loading": [
            "load application images into an isolated sandbox",
            "restore initial application data",
            "start services",
            "install packaged tools and verifier",
            "check initial state",
        ],
        "cleanup": "terminate task sandbox and its application containers; retain image cache and run results",
    }


def export_docker_context(record, output):
    """Export the software dependency recipe and public task workspace."""
    if record.category not in ENGINEERING:
        raise ValueError(
            "docker-context accepts software tasks; business tasks load their prepared application snapshots"
        )
    from mac_bench.environments.packaging.bundle import WorldBundle

    world = WorldBundle.load(record.bundle)
    environment = read_environment(world.root)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    base = output / "base"
    task = output / "task"
    base.mkdir()
    task.mkdir()
    (base / "setup.sh").write_text("#!/bin/sh\nset -eu\n" + environment["setup"] + "\n")
    dependency_lines = ""
    if environment.get("dependency_contents"):
        for name, content in environment["dependency_contents"].items():
            path = base / "dependencies" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (base / "dependencies.sh").write_text(environment["dependency_setup"])
        dependency_lines = "COPY dependencies/ /opt/mac-bench-dependencies/\nCOPY dependencies.sh /tmp/dependencies.sh\nRUN sh /tmp/dependencies.sh\n"
    (base / "Dockerfile").write_text(
        "FROM "
        + environment["image"]
        + "\nWORKDIR /work\nCOPY setup.sh /tmp/setup.sh\nRUN sh /tmp/setup.sh\n"
        + dependency_lines
    )
    shutil.copyfile(
        world.root / world.manifest["metadata"]["workspace_archive"],
        task / "source.tar",
    )
    public = task / "public"
    public.mkdir()
    for name in world.manifest["public_files"]:
        target = task / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(world.root / name, target)
    metadata = world.manifest["metadata"]
    fixture = metadata.get("fixture_archive")
    fixture_copy = ""
    if fixture:
        shutil.copyfile(world.root / fixture, task / "fixtures.tar")
        fixture_copy = 'COPY fixtures.tar /work/fixtures.tar\n'
    (task / "initialize.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /work/project\ntar xf /work/source.tar -C /work/project\n"
        + initialization_commands(environment, metadata)
    )
    (task / "Dockerfile").write_text(
        'ARG BASE_IMAGE=mac-bench-software-base\nFROM ${BASE_IMAGE}\nCOPY source.tar /work/source.tar\nCOPY public /work/public\n'
        + fixture_copy + 'COPY initialize.sh /tmp/initialize.sh\nRUN sh /tmp/initialize.sh\nRUN cp /work/public/task.txt /work/TASK.md\nWORKDIR /work/project\nCMD ["/bin/bash"]\n'
    )
    (output / "task.json").write_text(
        json.dumps(
            {
                "task_id": record.task_id,
                "world_revision": record.world_revision,
                "base_image": environment["image"],
                "use": "software workspace build materials",
            },
            indent=2,
        )
        + "\n"
    )
    (output / "README.md").write_text("""# Software task workspace

Build from this directory:

```bash
docker build -t mac-bench-software-base base
docker build -t mac-bench-software-task task
docker run --rm -it --network none mac-bench-software-task
```

The base image runs the packaged dependency setup. The task image loads the
starter, public materials and task initialization. Read `/work/TASK.md` and
`/work/public/` inside the container. These build materials are separate from
the evaluation harness and do not include private tests or reference code.
""")
    return output


def check_task(record, output, *, task_factory=open_task):
    task = task_factory(record, output)
    try:
        task.open()
        initial = {k:v for k,v in task.assert_initial().items() if k != "observation"}
        task.reset()
        reset = {k:v for k,v in task.assert_initial().items() if k != "observation"}
        return {"task_id": record.task_id, "initial": initial, "reset": reset}
    finally:
        task.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("describe", "check", "prepare", "docker-context")
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild a software dependency image")
    args = parser.parse_args(argv)
    if args.rebuild and args.command != 'prepare':
        parser.error('--rebuild is used with environment prepare')
    base, rows = catalog_rows(repository_root(), [args.task_id])
    row = rows[0]
    record = task_record(
        base / row["bundle"],
        category=row["category"],
        split=row["split"],
        task_id=row["task_id"],
        revision=row["world_revision"],
    )
    if args.command == "describe":
        print(json.dumps(describe(record.bundle, record.category), indent=2))
        return 0
    if args.command == "docker-context":
        if args.out is None:
            parser.error("docker-context requires --out")
        print(export_docker_context(record, args.out))
        return 0
    output = args.out or artifact_root() / "environment" / str(uuid4())
    if args.command == "prepare":
        if record.category not in ENGINEERING:
            parser.error(
                "Business environments are restored per task; use environment check"
            )
        from mac_bench.environments.engineering.preparation import (
            prepare_environment,
        )

        environment = read_environment(record.bundle)
        result = {
            "task_id": record.task_id,
            "image": asdict(prepare_environment(environment, output, backend=create_backend(resolve_config("software")), rebuild=args.rebuild)),
        }
    else:
        result = check_task(record, output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
