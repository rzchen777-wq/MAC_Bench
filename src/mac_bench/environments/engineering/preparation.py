"""Prepare locked software dependencies once, independently of task source."""

import json
from pathlib import Path
import tempfile
from mac_bench.runtime.sandbox.images import (
    recipe_key,
    prepare_cached_image,
    write_record,
)


def read_environment(directory):
    directory = Path(directory)
    environment = json.loads((directory / "private/environment.json").read_text())
    if environment.get("dependency_files"):
        environment["dependency_contents"] = {
            name: (directory / path).read_text()
            for name, path in environment["dependency_files"].items()
        }
    return environment


def dependency_recipe(environment):
    recipe = {"image": environment["image"], "setup": environment["setup"]}
    if environment.get("dependency_contents"):
        recipe.update(
            dependency_setup=environment["dependency_setup"],
            dependency_contents=environment["dependency_contents"],
            dependency_preparation="single-sandbox",
        )
    return recipe_key(recipe)


def prepare_environment(environment, run, *, backend, rebuild=False):
    recipe = dependency_recipe(environment)

    def build(directory):
        from .product.runtime import ProductRuntime

        with tempfile.TemporaryDirectory(prefix="prepare-", dir=directory) as temporary:
            runtime = ProductRuntime(environment, temporary, backend=backend)
            try:
                runtime.open()
                result = runtime.command(
                    "setup",
                    environment["setup"],
                    timeout=environment.get("setup_timeout", 1800),
                )
                if result.exit_code:
                    raise RuntimeError(
                        "Dependency setup failed: " + result.stdout[-4000:]
                    )
                if environment.get("dependency_contents"):
                    backend.write_files(
                        runtime.sandbox,
                        files={
                            "/opt/mac-bench-dependencies/" + filename: content.encode()
                            for filename, content in environment[
                                "dependency_contents"
                            ].items()
                        },
                    )
                    result = runtime.command(
                        "dependencies",
                        environment["dependency_setup"],
                        timeout=environment.get("setup_timeout", 1800),
                    )
                    if result.exit_code:
                        raise RuntimeError(
                            "Locked dependency setup failed: " + result.stdout[-4000:]
                        )
                return backend.capture_image(
                    runtime.sandbox, recipe=recipe, timeout=1200
                )
            finally:
                runtime.close()

    image = prepare_cached_image(
        backend, kind="software", recipe=recipe, build=build, rebuild=rebuild
    )
    write_record(Path(run) / "environment/image.json", image)
    return image
