"""Behavioral regressions for release-alignment fixes, independent of model runs."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import pytest

from mac_bench.runtime.paths import repository_root

TASKS = Path(os.environ.get('MAC_BENCH_TASK_ROOT', repository_root() / 'data/tasks'))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('body,passed',[
    ('def test_ok(): pass',True),
    ('import pytest\ndef test_ok(): pass\n@pytest.mark.skip(reason="required")\ndef test_needed(): pass',False),
    ('import pytest\n@pytest.mark.skip(reason="all")\ndef test_needed(): pass',False),
    ('def test_bad(): assert False',False),
    ('',False),
])
def test_required_pytest_outcomes(tmp_path,body,passed):
    runner=load('test_runner',TASKS/'jinja_templates/private/verifier/candidate_runner.py')
    p=tmp_path/'test_sample.py';p.write_text(body)
    result=runner.run_candidate_tests([sys.executable,'-m','pytest','-q','-o','addopts=',str(p)],cwd=tmp_path,text=True,timeout=30,env=None)
    assert (result.returncode==0)==passed,(result.stdout,result.stderr)


def test_allowed_skip_requires_exact_id(tmp_path):
    runner=load('test_runner',TASKS/'jinja_templates/private/verifier/candidate_runner.py')
    p=tmp_path/'test_sample.py';p.write_text('import pytest\ndef test_ok():pass\n@pytest.mark.skip(reason="platform")\ndef test_optional():pass')
    args=[sys.executable,'-m','pytest','-q','-o','addopts=',str(p)]
    # Determine the actual collected ID, not a substring or arbitrary skip reason.
    first=runner.run_candidate_tests(args,cwd=tmp_path,text=True,timeout=30)
    assert first.returncode!=0
    optional=next(k for k,v in first.execution_report['outcomes'].items() if v=='skipped')
    allowed=runner.run_candidate_tests(args,cwd=tmp_path,text=True,timeout=30,allowed_skips=[optional])
    assert allowed.returncode==0


@pytest.mark.parametrize('skip',[False,True])
def test_sqlglot_group_requires_executed_tests(skip):
    source=TASKS/'sqlglot_optimizer/private/verifier/check_platform.py'
    node=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='main')
    native=load('native_results',source.parent/'native_results.py')
    class Case(unittest.TestCase):
        def test_case(self):
            if skip:self.skipTest('missing behavior')
    namespace=dict(sys=sys,io=io,unittest=unittest,GROUPS={'expressions':['case']},RecordedResult=native.RecordedResult,test_ids=native.test_ids)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
    with patch.object(sys,'argv',['check','--group','expressions']),patch.object(unittest.defaultTestLoader,'loadTestsFromName',side_effect=lambda _:unittest.defaultTestLoader.loadTestsFromTestCase(Case)),contextlib.redirect_stdout(io.StringIO()):
        assert namespace['main']()==int(skip)


def test_background_job_reports_real_exit_code(tmp_path):
    script=TASKS/'black_formatter/public/development_service.py'
    command=[sys.executable,str(script),'--state',str(tmp_path/'job.json')]
    started=subprocess.run(command+['job','--',sys.executable,'-c','import time;time.sleep(.1);print("saved output");raise SystemExit(7)'],text=True,capture_output=True,check=True)
    record=json.loads(started.stdout)
    for _ in range(100):
        value=json.loads((tmp_path/'job.json').read_text())
        if value.get('completed'):break
        time.sleep(.02)
    status=subprocess.run(command+['job-status'],capture_output=True,text=True)
    assert status.returncode==1
    assert json.loads(status.stdout)['exit_code']==7
    assert 'saved output' in Path(record['log_path']).read_text()


def test_service_persists_and_stops(tmp_path):
    script=TASKS/'black_formatter/public/development_service.py'
    command=[sys.executable,str(script),'--state',str(tmp_path/'service.json')]
    try:
        result=subprocess.run(command+['start','--',sys.executable,'-c','import time;time.sleep(30)'],capture_output=True,text=True,check=True)
        assert json.loads(result.stdout)['running']
        again=subprocess.run(command+['status'],capture_output=True,text=True,check=True)
        assert json.loads(again.stdout)['running']
    finally:
        subprocess.run(command+['stop'],capture_output=True,text=True,check=True)
    time.sleep(.1)
    stopped=subprocess.run(command+['status'],capture_output=True,text=True)
    assert stopped.returncode==1 and not json.loads(stopped.stdout)['running']


def test_workflow_action_accepts_equivalent_names():
    playwright=pytest.importorskip('playwright.sync_api')
    sync_playwright=playwright.sync_playwright
    source=TASKS/'paperless_workflows/private/verifier/check_product.py'
    nodes=[n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in ['ui_name','action_control']]
    namespace={};exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),namespace)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        try:
            page=browser.new_page()
            for tag,label in [('button','Add Workflow'),('button','Create workflow'),('a','New workflow')]:
                page.set_content(f'<{tag} href="#" onclick="document.body.dataset.saved=1">{label}</{tag}>')
                namespace['action_control'](page,'Add Workflow','Create workflow','New workflow').click()
                assert page.locator('body').get_attribute('data-saved')=='1'
            page.set_content('<button>Delete workflow</button>')
            with pytest.raises(AssertionError):namespace['action_control'](page,'Add Workflow','Create workflow','New workflow')
        finally:browser.close()


def test_collection_skip_cannot_hide_required_module(tmp_path):
    runner=load('test_runner',TASKS/'jinja_templates/private/verifier/candidate_runner.py')
    (tmp_path/'test_ok.py').write_text('def test_ok(): pass')
    (tmp_path/'test_required.py').write_text('import pytest\npytest.skip("not implemented", allow_module_level=True)')
    result=runner.run_candidate_tests([sys.executable,'-m','pytest','-q','-o','addopts=',str(tmp_path)],cwd=tmp_path,text=True,timeout=30)
    assert result.returncode!=0
    assert any(k.endswith('test_required.py') and v=='skipped' for k,v in result.execution_report['outcomes'].items())
