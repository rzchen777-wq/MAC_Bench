"""Public task material integrity and developer-entry regression checks."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

import pytest

from mac_bench.runtime.paths import repository_root

ROOT = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root() / 'data/tasks'))
CATALOG = json.loads((repository_root() / 'data/tasks.json').read_text())['tasks']
TASKS = [ROOT / row['task_id'] for row in CATALOG
         if row['category'] in {'product_application', 'platform_data', 'developer_release'}]


@pytest.mark.parametrize('task', TASKS, ids=lambda p:p.name)
def test_task_navigation_and_shell_commands(task):
    manifest=json.loads((task/'manifest.json').read_text())
    text=(task/'public/task.txt').read_text()
    assert 'v2 migration guide' not in text
    assert not (task/'public/fixtures.txt').exists()
    for name in manifest['public_files']:
        assert (task/name).is_file()
    with tarfile.open(task/'public/workspace.tar') as archive:
        names=set(archive.getnames())
    for path in re.findall(r'^- `([^`]+)` —',text,re.M):
        assert path in names or any(n.startswith(path.rstrip('/')+'/') for n in names),path
    for path in re.findall(r'`(/work/public/[^`]+)`',text):
        assert (task/path.removeprefix('/work/')).is_file(),path
    for script in (task/'public').glob('*.sh'):
        subprocess.run(['sh','-n',script],check=True)
    script=(task/'public/develop.sh').read_text()
    for test in re.findall(r'(?:tests|test|flit_core/tests_core|networkx/algorithms)/[\w/.-]+',script):
        assert test in names or any(n.startswith(test.rstrip('/')+'/') for n in names),test


def test_documentation_is_available_offline():
    for task,expected in {'lark_engine':'docs/grammar.md','cattrs_conversion':'docs/structuring.md',
                          'hatchling_packages':'docs/config/build.md'}.items():
        with tarfile.open(ROOT/task/'public/workspace.tar') as archive:
            assert archive.getmember(expected).isfile()


@pytest.mark.parametrize('task_name',['freshrss_labels','kanboard_project_roles','mealie_households','navidrome_multilibrary','paperless_workflows'])
def test_fixture_locator_resolves_under_real_extraction_root(task_name):
    p=ROOT/task_name; manifest=json.loads((p/'manifest.json').read_text())
    destination=manifest['metadata'].get('fixture_directory','/work')
    with tarfile.open(p/'public/fixtures.tar') as archive:
        available={str(Path(destination)/name) for name in archive.getnames()}
    guide=(p/'public/fixtures.md').read_text()
    for path in re.findall(r'`(/work/test-state/[^`]+\.json)`',guide):
        assert path in available
    script=(p/'public/develop.sh').read_text()
    assert '/work/dev-data' not in script and '/work/reading-fixture' not in script


@pytest.mark.parametrize('default_lock',[False,True])
def test_mealie_recipe_check_sets_both_policy_states(default_lock):
    path=ROOT/'mealie_households/private/verifier/check_product.py'
    tree=ast.parse(path.read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='recipes')
    namespace={};exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
    class App:
        def __init__(self):self.lock=default_lock;self.description='';self.states=[]
        def ensure_scope(self):return {'householdId':'A'},'a',{'householdId':'B'},'b',{}
        def call(self,method,path,auth=None,json=None,expected=200):
            if path=='/api/admin/households/A':
                if method=='PUT':self.lock=json['preferences']['lockRecipeEditsFromOtherHouseholds'];self.states.append(self.lock)
                return {'preferences':{'lockRecipeEditsFromOtherHouseholds':self.lock}}
            if method=='POST':return {'householdId':'B'} if path.endswith('/duplicate') else 'sample'
            if method=='PUT':
                assert (403 if self.lock and auth=='b' else 200)==expected
                if expected==200:self.description=json['description']
            return {'householdId':'A','description':self.description}
    app=App();namespace['recipes'](app);assert app.states==[True,False]


def test_initializer_extracts_before_setup_and_build(tmp_path):
    from mac_bench.environments.engineering.public_workspace import initialization_commands
    work=tmp_path/'work';work.mkdir()
    src=tmp_path/'db';src.write_text('original')
    with tarfile.open(work/'fixtures.tar','w') as t:t.add(src,arcname='data/db.sqlite')
    env={'public_prepare':'test -f /work/test-state/data/db.sqlite; echo configured > /work/project/config',
         'build':'test -f config; test "$(cat /work/test-state/data/db.sqlite)" = original'}
    cmd=initialization_commands(env,{'fixture_archive':'public/fixtures.tar','fixture_directory':'/work/test-state'})
    subprocess.run(['sh','-c',cmd.replace('/work',str(work))],check=True)


def test_docker_uses_the_same_fixture_and_preparation_paths(tmp_path):
    cli = pytest.importorskip('mac_bench.environment_cli')
    from mac_bench.environments.packaging.bundle import seal_bundle
    from mac_bench.evaluation.tasks.catalog import task_record
    root = tmp_path / 'bundle'
    (root / 'public').mkdir(parents=True)
    (root / 'private').mkdir()
    (root / 'public/task.txt').write_text('Use /work/test-state/data')
    (root / 'public/prepare.sh').write_text('test -f /work/test-state/data/db.sqlite\n')
    (root / 'public/source.tar').write_bytes(b'source archive')
    (root / 'public/fixtures.tar').write_bytes(b'fixture archive')
    (root / 'private/reference.tar').write_bytes(b'private solution')
    env = {'image': 'python:3.12-slim', 'setup': 'true', 'build': 'echo build',
           'public_prepare': 'sh /work/public/prepare.sh'}
    (root / 'private/environment.json').write_text(json.dumps(env))
    seal_bundle(root, world_id='sample', public_files=['public/task.txt', 'public/prepare.sh'],
                metadata={'world_family': 'product_application', 'workspace_archive': 'public/source.tar',
                          'fixture_archive': 'public/fixtures.tar', 'fixture_directory': '/work/test-state'})
    output = cli.export_docker_context(task_record(root, category='product_application'), tmp_path / 'docker')
    script = (output / 'task/initialize.sh').read_text()
    assert 'tar xf /work/fixtures.tar -C /work/test-state' in script
    assert script.index('tar xf /work/fixtures.tar') < script.index('sh /work/public/prepare.sh') < script.index('echo build')
    assert (output / 'task/fixtures.tar').read_bytes() == b'fixture archive'
    assert 'COPY fixtures.tar /work/fixtures.tar' in (output / 'task/Dockerfile').read_text()
    assert not any(p.name == 'reference.tar' or 'private' in p.relative_to(output).parts for p in output.rglob('*'))
