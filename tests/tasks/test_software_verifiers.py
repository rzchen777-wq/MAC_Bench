"""Behavioral regression checks against the current packaged software graders."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile

import pytest


from mac_bench.runtime.paths import repository_root

TASKS = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root() / 'data/tasks'))

def module(path):
    spec = importlib.util.spec_from_file_location('task_probe_' + path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def grader(name, filename='check_platform.py'):
    return TASKS / name / 'private/verifier' / filename


def relocate(name, filename, tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    target = work / 'grader'
    shutil.copytree(grader(name).parent, target)
    for p in target.glob('*.py'):
        p.write_text(p.read_text().replace('/work/', str(work) + '/'))
    return work, target / filename


def execute(script, work, *args):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(work / 'project/src') + os.pathsep + str(work / 'project')}
    return subprocess.run([sys.executable, str(script), *args], cwd=work,
                          env=env, capture_output=True, text=True, timeout=30)


def test_timeout_keeps_output_and_returns_failure():
    runner = module(grader('lark_engine', 'candidate_runner.py'))
    result = runner.run_candidate_tests(
        [sys.executable, '-u', '-c', 'import time; print("started"); time.sleep(10)'],
        capture_output=True, text=True, timeout=0.3)
    assert result.returncode == 1
    assert 'started' in result.stdout and 'timeout' in result.stderr


def test_launch_failure_is_not_a_candidate_failure():
    runner = module(grader('lark_engine', 'candidate_runner.py'))
    with pytest.raises(FileNotFoundError):
        runner.run_candidate_tests(['/no/such/test-runner'], timeout=1)


def test_timeout_kills_worker_descendants(tmp_path):
    runner = module(grader('lark_engine', 'candidate_runner.py'))
    marker = tmp_path / 'leaked'
    child = f'import time,pathlib; time.sleep(.8); pathlib.Path({str(marker)!r}).touch()'
    parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(10)'
    result = runner.run_candidate_tests([sys.executable, '-c', parent],
                                        text=True, timeout=.3)
    assert result.returncode == 1
    # A second bounded process gives an orphan enough time to reveal itself.
    subprocess.run([sys.executable, '-c', 'import time; time.sleep(1)'], check=True)
    assert not marker.exists()


@pytest.mark.parametrize('name', ['networkx_routing', 'lark_engine'])
def test_each_timed_out_goal_is_recorded(name, tmp_path):
    work, script = relocate(name, 'check_platform.py', tmp_path)
    if name == 'lark_engine':
        with tarfile.open(TASKS / name / 'private/reference.tar') as archive:
            archive.extractall(work / 'project', filter='data')
    driver = script.parent / 'timeout_probe.py'
    driver.write_text('import runpy, sys, candidate_runner\n'
                      'original = candidate_runner.run_candidate_tests\n'
                      'def timed_out(*args, **kwargs):\n'
                      '    return original([sys.executable,"-c","import time; time.sleep(5)"], text=True, timeout=.05)\n'
                      'candidate_runner.run_candidate_tests = timed_out\n'
                      f'runpy.run_path({str(script)!r}, run_name="__main__")\n')
    result = execute(driver, work)
    checks = json.loads((script.parent / 'result.json').read_text())['checks']
    assert result.returncode == 1, result.stderr
    assert len(checks) == 6 and all(c['status'] == 'failed' for c in checks)


@pytest.mark.parametrize('variant', ['reference', 'starter', 'unusable_forest', 'lost_ambiguity'])
def test_lark_forest_contract(variant, tmp_path):
    work, script = relocate('lark_engine', 'check_platform.py', tmp_path)
    archive_name = 'public/workspace.tar' if variant == 'starter' else 'private/reference.tar'
    with tarfile.open(TASKS / 'lark_engine' / archive_name) as archive:
        archive.extractall(work / 'project', filter='data')
    source = work / 'project/lark/lark.py'
    raw = source.read_text()
    needle = '        return self.parser.parse(text, start=start, on_error=on_error)'
    if variant == 'unusable_forest':
        assert raw.count(needle) == 1
        source.write_text(raw.replace(needle, '        if self.options.ambiguity == "forest":\n            return object()\n' + needle))
    elif variant == 'lost_ambiguity':
        source = work / 'project/lark/parsers/earley_forest.py'
        raw = source.read_text()
        needle = 'self.resolve_ambiguity = resolve_ambiguity'
        assert needle in raw
        source.write_text(raw.replace(needle, 'self.resolve_ambiguity = True'))
    result = execute(script, work)
    checks = {c['id']: c['status'] for c in json.loads((script.parent / 'result.json').read_text())['checks']}
    if variant == 'reference':
        assert result.returncode == 0 and all(v == 'passed' for v in checks.values()), result.stdout + result.stderr
    elif variant == 'starter':
        assert checks['regression'] == 'passed'
        assert all(v == 'failed' for k, v in checks.items() if k != 'regression')
    else:
        assert checks['ambiguity'] == 'failed'
        assert checks['regression'] == 'passed'


@pytest.mark.parametrize('name', ['isort_imports', 'black_formatter', 'virtualenv_environments'])
def test_broken_candidate_import_returns_case_results(name, tmp_path):
    work, script = relocate(name, 'produce.py', tmp_path)
    package_name = name.split('_')[0]
    package = work / ('project' if name == 'isort_imports' else 'project/src') / package_name
    package.mkdir(parents=True)
    error = 'SystemExit(2)' if name == 'virtualenv_environments' else 'RuntimeError("broken candidate")'
    (package / '__init__.py').write_text('raise ' + error + '\n')
    result = execute(script, work)
    observed = json.loads((script.parent / 'producer-result.json').read_text())
    cases = json.loads((script.parent / 'cases.json').read_text())
    assert result.returncode == 0, result.stderr
    assert len(observed['cases']) == len(cases)
    assert all(c['status'] == 'failed' for c in observed['cases'])
    assert observed['regression']['status'] == 'failed'


def test_mealie_candidate_data_and_trusted_errors_are_distinct():
    m = module(grader('mealie_households', 'check_product.py'))
    good = m.candidate_json({'id': 7, 'items': [{'name': 'Ada'}]}, 'GET /example')
    assert good['id'] == 7 and good['items'][0]['name'] == 'Ada'
    with pytest.raises(AssertionError, match='missing required candidate field'):
        good['items'][0]['id']
    with pytest.raises(KeyError):
        {}['missing_trusted_fixture_field']


@pytest.mark.parametrize('payload,valid', [({'id': 7}, True), ([], False), ('bad', False)])
def test_mealie_wrong_http_envelope_is_a_failure(payload, valid, tmp_path):
    from types import SimpleNamespace
    m = module(grader('mealie_households', 'check_product.py'))
    app = m.Product(tmp_path)
    app.admin = {}
    app.session = SimpleNamespace(request=lambda *a, **k: SimpleNamespace(
        status_code=200, text=json.dumps(payload), json=lambda: payload))
    if valid:
        assert app.call('GET', '/api/households/self')['id'] == 7
    else:
        with pytest.raises(AssertionError):
            app.call('GET', '/api/households/self')


@pytest.mark.parametrize('layout', ['popup', 'inline'])
@pytest.mark.parametrize('broken_filter', [False, True])
def test_library_selector_accepts_different_visible_text_and_layout(layout, broken_filter, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    checker = grader('navidrome_multilibrary', 'check_product.py')
    monkeypatch.syspath_prepend(str(checker.parent))
    # Execute the exact nested acceptance check, including its DOM outcome tests.
    node = next(n for n in ast.walk(ast.parse(checker.read_text()))
                if isinstance(n, ast.FunctionDef) and n.name == 'ui_selector')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page()
            page.set_default_timeout(2000)
            hidden = 'hidden' if layout == 'popup' else ''
            html = f'''<button aria-label="Libraries" onclick="panel.hidden=false">Choose collections</button>
                <section id="panel" {hidden}><article><label>Every collection
                <input id="all" aria-label="All libraries" type="checkbox" onchange="other.checked=third.checked=this.checked; update()"></label></article>
                <aside><label><input id="other" type="checkbox" aria-label="Original collection">My collection</label>
                <label><input id="third" type="checkbox" aria-label="Third Collection" onchange="update()">Another collection</label></aside></section>
                <a role="menuitem" href="#/song" onclick="panel.hidden={str(layout == 'popup').lower()}">Songs</a>
                <main id="songs"></main><script>function update() {{songs.textContent={'true' if broken_filter else 'third.checked'}?'Private Track':''}}</script>'''
            page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
            def observed(fn, description):
                assert fn(), description
            namespace = {'page': page, 'BASE': 'http://task.test', 'wait_for': observed}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(checker), 'exec'), namespace)
            if broken_filter:
                with pytest.raises(AssertionError, match='selected library filter'):
                    namespace['ui_selector']()
            else:
                namespace['ui_selector']()
        finally:
            browser.close()


@pytest.mark.parametrize('task,package', [('lark_engine','lark'), ('marshmallow_schemas','marshmallow'),
                                         ('cattrs_conversion','cattrs'), ('rdflib_datasets','rdflib')])
@pytest.mark.parametrize('exception', ['RuntimeError("broken import")', 'SystemExit(0)', 'SystemExit(2)'])
def test_platform_import_failure_produces_failed_checks(task, package, exception, tmp_path):
    work, script = relocate(task, 'check_platform.py', tmp_path)
    package_dir = work / 'project/src' / package
    if task == 'lark_engine':
        package_dir = work / 'project' / package
    package_dir.mkdir(parents=True)
    (package_dir / '__init__.py').write_text('raise ' + exception)
    result = execute(script, work)
    assert result.returncode == 1, result.stderr
    checks = json.loads((script.parent / 'result.json').read_text())['checks']
    assert len(checks) == 6 and all(c['status'] == 'failed' for c in checks)


@pytest.mark.parametrize('task,mutation,failed_group', [
    ('lark_engine', '', None),
    ('lark_engine', 'import lark\noriginal=lark.Lark.__init__\ndef broken(self,*args,**kwargs):\n kwargs.pop("lexer_callbacks",None)\n original(self,*args,**kwargs)\nlark.Lark.__init__=broken\n', 'lexing'),
    ('marshmallow_schemas', '', None),
    ('marshmallow_schemas', 'from marshmallow import fields\nfields.Float._serialize=lambda *a,**k:None\n', 'codecs'),
    ('marshmallow_schemas', 'from marshmallow import fields\nfields.Boolean._serialize=lambda *a,**k:None\n', 'codecs'),
])
def test_public_platform_contracts(task, mutation, failed_group, tmp_path):
    work, script = relocate(task, 'check_platform.py', tmp_path)
    with tarfile.open(TASKS / task / 'private/reference.tar') as archive:
        archive.extractall(work / 'project', filter='data')
    source = work / ('project' if task == 'lark_engine' else 'project/src')
    (source / 'sitecustomize.py').write_text(mutation)
    result = execute(script, work)
    checks = {c['id']: c for c in json.loads((script.parent / 'result.json').read_text())['checks']}
    if failed_group:
        assert result.returncode == 1 and checks[failed_group]['status'] == 'failed', checks
    else:
        assert result.returncode == 0 and all(c['status'] == 'passed' for c in checks.values()), checks


@pytest.mark.parametrize('task', ['flit_packages', 'hatchling_packages'])
@pytest.mark.parametrize('variant', ['sha256','sha512','signature','missing','blank','wrong_size','duplicate','weak_hash'])
def test_wheel_record_contract(task, variant):
    import base64, csv, hashlib, io, zipfile
    validate = module(grader(task, 'wheel_records.py')).validate_wheel_record
    files = {'demo/__init__.py': b'VALUE=3\n', 'demo-1.dist-info/METADATA': b'Name: demo\n'}
    record = 'demo-1.dist-info/RECORD'
    algorithm = 'sha512' if variant == 'sha512' else 'sha1' if variant == 'weak_hash' else 'sha256'
    rows = [[name, algorithm + '=' + base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).decode().rstrip('='), str(len(data))] for name, data in files.items()]
    if variant == 'missing': rows.pop()
    if variant == 'blank': rows[0][1] = ''
    if variant == 'wrong_size': rows[0][2] = '999'
    if variant == 'duplicate': rows.append(rows[0])
    rows.append([record,'',''])
    data = io.StringIO(newline=''); csv.writer(data).writerows(rows)
    files[record] = data.getvalue().encode()
    if variant == 'signature': files[record+'.jws'] = b'signature'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items(): archive.writestr(name, data)
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as archive:
        if variant in {'sha256','sha512','signature'}: validate(archive)
        else:
            with pytest.raises(AssertionError): validate(archive)


@pytest.mark.parametrize('variant', ['normal','equivalent_quotes','broken_render'])
def test_jinja_compilation_accepts_behavior_not_source_spelling(variant, tmp_path):
    work, script = relocate('jinja_templates', 'test_compile_behavior.py', tmp_path)
    with tarfile.open(TASKS / 'jinja_templates/private/reference.tar') as archive:
        archive.extractall(work / 'project', filter='data')
    if variant == 'equivalent_quotes':
        patch = r'''import re
from jinja2 import Environment
original=Environment.compile
def equivalent(*args,**kwargs):
 result=original(*args,**kwargs)
 if isinstance(result,str): result=re.sub(r"filters\['(filter\d+)'\]",r'filters["\1"]',result)
 return result
Environment.compile=equivalent
'''
    elif variant == 'broken_render':
        patch = 'from jinja2 import Template\nTemplate.render=lambda *a,**k:"incorrect"\n'
    else: patch = ''
    (work / 'project/src/sitecustomize.py').write_text(patch)
    result = subprocess.run([sys.executable,'-m','pytest','-q','-o','addopts=',str(script)], cwd=work,
                            env={**os.environ,'PYTHONPATH':str(work/'project/src'),'PYTHONDONTWRITEBYTECODE':'1'},
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == (1 if variant == 'broken_render' else 0), result.stdout+result.stderr


@pytest.mark.parametrize('source,code', [('def test_ok(): assert True',0),
                                        ('import pytest\n@pytest.mark.skip(reason="missing")\ndef test_skip(): pass',1),
                                        ('',1), ('def test_bad(): assert False',1)])
def test_pytest_requires_executed_tests(source, code, tmp_path):
    (tmp_path/'test_sample.py').write_text(source)
    runner = module(grader('lark_engine','candidate_runner.py'))
    result = runner.run_candidate_tests([sys.executable,'-m','pytest','-q',str(tmp_path)], text=True,timeout=30)
    assert result.returncode == code, result.stdout+result.stderr
    if source:
        assert len(result.test_report) == 1


@pytest.mark.parametrize('variant', ['passed','skipped','empty','missing','interrupted','subtest'])
def test_unittest_results_require_all_collected_cases(variant, tmp_path):
    import io, unittest
    helper = module(grader('linkding_bookmarks','native_results.py'))
    class Case(unittest.TestCase):
        def runTest(self):
            if variant == 'skipped': self.skipTest('candidate skipped behavior')
            if variant == 'subtest':
                with self.subTest(value=1): self.assertEqual(1,2)
    suite = unittest.TestSuite([] if variant == 'empty' else [Case()])
    expected = list(helper.test_ids(suite))
    result = unittest.TextTestRunner(stream=io.StringIO(),resultclass=helper.RecordedResult).run(suite)
    report = {'expected':expected,'outcomes':result.outcomes,'completed':variant!='interrupted'}
    if variant == 'missing': report['outcomes']={}
    path=tmp_path/'results.json';path.write_text(json.dumps(report))
    status,_=helper.grade_report(path,0)
    assert status == ('passed' if variant=='passed' else 'failed')


@pytest.mark.parametrize('task,package', [('hatchling_packages','hatchling'),('flit_packages','flit_core'),
                                         ('installer_wheels','installer'),('datamodel_jsonschema','datamodel_code_generator')])
@pytest.mark.parametrize('exception', ['RuntimeError("broken import")','SystemExit(0)'])
def test_producer_import_errors_are_reported(task,package,exception,tmp_path):
    work,script=relocate(task,'produce.py',tmp_path)
    package_dir=work/'project'/package;package_dir.mkdir(parents=True)
    (package_dir/'__init__.py').write_text('raise '+exception)
    result=execute(script,work)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((script.parent/'producer-result.json').read_text())
    assert report['cases'] and all(c['status']=='failed' for c in report['cases'])
    assert report['regression']['status']=='failed'


@pytest.mark.parametrize('layout', ['table','section'])
@pytest.mark.parametrize('created', [True,False])
def test_role_discovery_uses_public_links(layout,created):
    from types import SimpleNamespace
    from urllib.parse import parse_qs,urlparse
    from bs4 import BeautifulSoup
    checker=grader('kanboard_project_roles','check_product.py')
    node=next(n for n in ast.walk(ast.parse(checker.read_text())) if isinstance(n,ast.FunctionDef) and n.name=='role')
    namespace={'BeautifulSoup':BeautifulSoup,'parse_qs':parse_qs,'urlparse':urlparse}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(checker),'exec'),namespace)
    fragment='<a href="?controller=ProjectRoleController&action=edit&project_id=7&role_id=19">Edit</a>'
    html=('<table><tr><th>Restrictions for the role "Reviewer"</th></tr><tr><td>'+fragment+'</td></tr></table>' if layout=='table'
          else '<section><h2>Reviewer</h2>'+fragment+'</section>')
    class Web:
        ready=False
        def form(self,*a,**k): self.ready=created
        def get(self,*a,**k): return SimpleNamespace(text=html if self.ready else '<main>No roles</main>')
    if created: assert namespace['role'](Web(),7,'Reviewer')==19
    else:
        with pytest.raises(AssertionError): namespace['role'](Web(),7,'Reviewer')


@pytest.mark.parametrize('code,body,mutate,accepted', [(400,{'error':'invalid'},False,True),
                                                      (500,{'error':'invalid'},False,True),
                                                      (500,'server crash',False,False),
                                                      (400,'At least one library is required',False,True),
                                                      (400,{'errors':{'name':'required'}},False,True),
                                                      (200,{'error':'invalid'},False,False),
                                                      (400,{'error':'invalid'},True,False)])
def test_library_invalid_input_checks_envelope_and_state(code,body,mutate,accepted):
    import copy
    checker=grader('navidrome_multilibrary','check_product.py')
    node=next(n for n in ast.walk(ast.parse(checker.read_text())) if isinstance(n,ast.FunctionDef) and n.name=='validation')
    state=[{'id':1,'name':'Default','path':'/music/default'}]
    def http(method,path,*a,**k):
        if method=='GET': return 403,{'error':'forbidden'}
        if mutate: state.append({'id':len(state)+1})
        return code,body
    namespace={'ROOT':Path('/tmp/reference'), 'token':'admin','user':{'token':'user'},
               'http':http,'api':lambda method,path,*a:copy.deepcopy(state[0] if path=='/library/1' else state),
               'rejected':module(checker).rejected}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(checker),'exec'),namespace)
    if accepted: namespace['validation']()
    else:
        with pytest.raises(AssertionError): namespace['validation']()


@pytest.mark.parametrize('layout', ['accessible','upstream','section'])
def test_workflow_combobox_does_not_require_component_css(layout):
    playwright=pytest.importorskip('playwright.sync_api')
    checker=grader('paperless_workflows','check_product.py')
    node=next(n for n in ast.walk(ast.parse(checker.read_text())) if isinstance(n,ast.FunctionDef) and n.name=='labelled_combobox')
    namespace={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(checker),'exec'),namespace)
    pages={
        'accessible':'<label for="sources">Filter sources</label><select id="sources"><option>API Upload</option></select>',
        'upstream':'<div class="paperless-input-select"><div><div><label for="unbound">Filter sources</label></div><div><div><input role="combobox" data-target="yes"></div></div></div></div>',
        'section':'<section><h2>Filter sources</h2><input role="combobox" data-target="yes"></section>',
    }
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page();page.set_content(pages[layout]+'<section><label>Other</label><input role="combobox"></section>')
            control=namespace['labelled_combobox'](page,'Filter sources')
            assert control.get_attribute('id')=='sources' or control.get_attribute('data-target')=='yes'
        finally: browser.close()
