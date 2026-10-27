"""Terminal source worlds for Platform & Data and Developer & Release tasks."""
import argparse
import json
import shlex
import tarfile
from pathlib import Path
from .product.runtime import LoadedProductWorld


class EngineeringCodeWorld(LoadedProductWorld):
    accepted_families = {'platform_data', 'developer_release'}

    def __init__(self, bundle, directory, *, backend=None):
        from mac_bench.environments.packaging.bundle import WorldBundle
        metadata = WorldBundle.load(bundle).manifest['metadata']
        if metadata.get('world_family') == 'developer_release':
            from .artifact_runtime import ArtifactRuntime
            self.runtime_type = ArtifactRuntime
        super().__init__(bundle, directory, backend=backend)


def smoke_world(world_path, output, *, world_type=EngineeringCodeWorld):
    with world_type(world_path, output) as world:
        with tarfile.open(world.bundle.root / world.meta['workspace_archive']) as archive:
            probe = next(m.name for m in archive.getmembers() if m.isfile())
        probe_path = shlex.quote('/work/project/' + probe)
        before = world.execute('sha256sum ' + probe_path)
        if before.exit_code:
            raise RuntimeError('Packaged source is not readable')
        if world.execute('test -s /work/TASK.md && test ! -e /work/grader').exit_code:
            raise RuntimeError('Agent workspace lacks its task or contains private verifier assets')
        sentinel = world.execute("printf altered > /work/project/.reset-sentinel; "
                                 "printf altered >> " + probe_path)
        if sentinel.exit_code:
            raise RuntimeError("Cannot write workspace")
        world.reset()
        if world.execute("test ! -e /work/project/.reset-sentinel").exit_code:
            raise RuntimeError("World reset did not restore a clean starter")
        after = world.execute('sha256sum ' + probe_path)
        if after.exit_code or after.stdout != before.stdout:
            raise RuntimeError('World reset did not restore original source contents')
        result = world.grade()
        if result.get("status") == "verifier_error":
            raise RuntimeError("Packaged starter cannot be graded")
        if (result.get('task_success') is not False
                or result.get('regression_passed') is not True
                or any(goal['passed'] for goal in result.get('goals', []))):
            raise RuntimeError('Packaged starter does not preserve the original construction conditions')
        reference = world.grade_submission(world.bundle.root / "private/reference.tar")
        if reference.get("task_success") is not True:
            raise RuntimeError("Packaged reference does not pass its complete verifier")
        from .product.artifacts import write_json
        write_json(Path(output) / "world_smoke.json", {"reset": "passed", "starter": result,
                   "reference": reference, "source_reset_probe": probe,
                   "agent_private_assets": "absent", "task": "loaded",
                   "world_revision": world.bundle.manifest["revision"]})
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args(argv)
    result=smoke_world(args.world,args.out)
    print(json.dumps({"status":"ready","starter_gc":result["gc"],"report":str(args.out/"world_smoke.json")},indent=2))
    return 0
