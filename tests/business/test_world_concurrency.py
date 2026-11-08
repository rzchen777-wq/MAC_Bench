"""Independent applications overlap while lifecycle and shared edits stay ordered."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace
import time

import pytest

from mac_bench.environments.tools import CallJournal, NativeResult, Role, Tool, ToolError, WorldTools
from mac_bench.environments.tools.compound import CompoundOperation
from mac_bench.environments.tools.contracts import object_schema
from mac_bench.environments.tools.registry import ToolRegistry
from mac_bench.environments.packaging.server import WorldAccess


def make_world(tmp_path, handlers):
    registry = ToolRegistry([Tool(name, app, app+'.record', 'change', app, {}, 'Test change',
        object_schema(), 'Native result', False, handler) for name, (app, handler) in handlers.items()], catalog_revision='test')
    journal = CallJournal(tmp_path / 'calls.sqlite')
    apps = {app for app, _ in handlers.values()}
    world = WorldTools('world', registry, [Role('operator', tuple(SimpleNamespace(application=a, scope={}) for a in apps), ('*',))], journal)
    return world, journal


def test_other_app_and_saved_receipt_do_not_wait_for_busy_app(tmp_path):
    started, release = Event(), Event()
    def slow(*_):
        started.set()
        assert release.wait(5)
        return NativeResult({'done': True})
    world, journal = make_world(tmp_path, {'slow': ('hrms', slow), 'fast': ('pretix', lambda *_: NativeResult({'value': 7}))})
    agent = world.agent('a', 'operator')
    saved = agent.call_tool('fast', {}, 'initial')
    with ThreadPoolExecutor(max_workers=3) as pool:
        future = pool.submit(agent.call_tool, 'slow', {}, 'slow')
        try:
            assert started.wait(2)
            assert pool.submit(agent.call_tool, 'fast', {}, 'fast').result(timeout=2)['status'] == 'succeeded'
            assert pool.submit(agent.read_result, request_id='initial', path='/data/value').result(timeout=2)['value'] == 7
        finally:
            release.set()
            future.result(timeout=2)
    assert len(journal.records('world')) == 3
    world.close(); journal.close()


def test_close_drains_admitted_compound_steps_but_blocks_new_work(tmp_path):
    started, release = Event(), Event()
    class Compound(CompoundOperation):
        applications = frozenset({"hrms"})
        def execute_agent(self, agent, args, call_id):
            started.set()
            assert release.wait(5)
            nested = agent.call_tool('native', {}, 'nested')
            return {'status': nested['status'], 'data': nested['data'], 'error': None}
    world, journal = make_world(tmp_path, {'compound': ('hrms', Compound()),
        'native': ('hrms', lambda *_: NativeResult({'done': True}))})
    agent = world.agent('a', 'operator')
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(agent.call_tool, 'compound', {}, 'outer')
        assert started.wait(2)
        close = pool.submit(world.close)
        try:
            deadline = time.monotonic()+2
            while not world.closed and time.monotonic() < deadline:
                time.sleep(.001)
            assert world.closed and not close.done()
            with pytest.raises(ToolError, match='closed'):
                agent.call_tool('native', {}, 'new')
        finally:
            release.set()
        assert future.result(timeout=2)['status'] == 'succeeded'
        close.result(timeout=2)
    assert len(journal.records('world')) == 2
    journal.close()


def test_owner_waits_for_agents_and_prevents_new_agents_overtaking():
    access = WorldAccess()
    first_entered, release_first, owner_entered, release_owner, second_entered = (Event() for _ in range(5))
    def first():
        with access.lease(owner=False):
            first_entered.set(); assert release_first.wait(5)
    def owner():
        with access.lease(owner=True):
            owner_entered.set(); assert release_owner.wait(5)
    def second():
        with access.lease(owner=False): second_entered.set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        a = pool.submit(first); assert first_entered.wait(2)
        o = pool.submit(owner)
        deadline = time.monotonic()+2
        while not access.waiting_owners and time.monotonic() < deadline: time.sleep(.001)
        b = pool.submit(second)
        try:
            assert access.waiting_owners == 1 and not owner_entered.is_set()
            assert not second_entered.wait(.05)
            release_first.set(); assert owner_entered.wait(2)
            assert not second_entered.wait(.05)
        finally:
            release_first.set(); release_owner.set()
        for f in (a,o,b): f.result(timeout=2)
    assert second_entered.is_set()


def test_cross_application_compounds_declare_locks_before_native_steps(tmp_path):
    class Linked(CompoundOperation):
        applications = frozenset({'hrms', 'twenty'})
        def __init__(self, order): self.order = order
        def execute_agent(self, agent, args, call_id):
            receipts = [agent.call_tool(name, {}, call_id + name) for name in self.order]
            return {'status': 'succeeded', 'data': [r['status'] for r in receipts], 'error': None}
    world, journal = make_world(tmp_path, {
        'forward': ('twenty', Linked(['employee', 'note'])),
        'reverse': ('hrms', Linked(['note', 'employee'])),
        'employee': ('hrms', lambda *_: NativeResult({'name': 'EMP-1'})),
        'note': ('twenty', lambda *_: NativeResult({'id': 'NOTE-1'})),
    })
    agent = world.agent('a', 'operator')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = [pool.submit(agent.call_tool, n, {}, n) for n in ('forward', 'reverse')]
            assert [f.result(timeout=3)['data'] for f in runs] == [['succeeded', 'succeeded']] * 2
        assert len(journal.records('world')) == 6
    finally:
        world.close(); journal.close()
