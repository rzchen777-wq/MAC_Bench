"""Adapt the real native world, keeping owner RPC and grading out of agent tools."""
import json
from copy import deepcopy
from pathlib import Path
from mac_bench.runtime.common.hashing import digest
from mac_bench.environments.packaging.bundle import WorldBundle
from mac_bench.environments.tools.api import SCHEMAS, DESCRIPTIONS
from ..protocol import TaskView, ToolDefinition
from .base import OwnedTask
from ..resources import PublicResources, LIST_RESOURCES, READ_RESOURCE, QUERY_RESOURCE, SEARCH_RESOURCE
from mac_bench.environments.tools.contracts import ToolError

GUIDE = '''# Role and Goal
Complete the user's task by making the required changes in the live business applications.
Use only the supplied tools. No shell, reference answer, or grading tool is available.

# Task Materials
Read public/task.txt in full before making changes. Follow next_offset to continue an incomplete read.
Use list_resources to find supporting files. Use query_resource to look up IDs in public JSON,
and search_resource to locate passages in public text files. Consult the ID mapping as needed.

# Application Tools
Discover applications with list_apps and operations with list_tools.
Before using an unfamiliar operation, read describe_tool. Copy operation names exactly,
including suffixes, and follow the documented input schema.
Identify records by their native IDs; names alone may be ambiguous.
For call_tool, use a new request_id for each new action or fresh read.
Reuse an ID only to recover the receipt for that same logical call.

# Results and Verification
Inspect operation results and read back current application records to verify changes.
An accepted or unknown receipt does not establish completion.
Use read_result or query_result to inspect a saved large result without executing the operation again.
Public files and saved receipts are reference material, not live application state.
A saved receipt and its aggregates cover only that response; retrieve additional application
pages through fresh application reads.

# Completion
Check the resulting application state before reporting completion.
Summarize completed work and any unresolved requirements.
A completion report does not change application state.'''


class OperationalTask(OwnedTask):
    def __init__(self, record, directory, *, backend=None, owner=None, bundle=None, session=None):
        self.record, self.directory = record, Path(directory)
        self.bundle = bundle or WorldBundle.load(record.bundle)
        if self.bundle.manifest.get('metadata', {}).get('tool_api_version', 2 if session else None) != 2:
            raise ValueError('Operational tasks require tool API v2')
        self._owner, self._borrowed, self._backend = owner, session, backend
        from mac_bench.environments.packaging.interface import host_components
        api, resources, guide, version = host_components(self.bundle)
        self.resources = resources.PublicResources(self.bundle)
        self._resource_errors = (ToolError, resources.ToolError)
        instructions = guide
        if 'public/tool_tutorial.md' in self.resources.names:
            instructions = guide.replace(
                'Consult the ID mapping as needed.',
                'Consult the ID mapping as needed. Read public/tool_tutorial.md for a worked tool-use example.')
        self._configure(TaskView(record.task_id, record.world_revision, self.resources.instruction(),
                          (resources.LIST_RESOURCES, resources.READ_RESOURCE, resources.QUERY_RESOURCE, resources.SEARCH_RESOURCE) + tuple(
                              ToolDefinition(k, api.DESCRIPTIONS[k], deepcopy(v)) for k, v in api.SCHEMAS.items()), instructions))

    @property
    def _session(self):
        return self._borrowed if self._borrowed is not None else self._owner.session

    def open(self):
        if self._opened:
            raise RuntimeError('Task is already open')
        try:
            if self._borrowed is None:
                if self._owner is None:
                    from mac_bench.environments.business.runtime import PortableBusinessWorld
                    self._owner = PortableBusinessWorld(self.bundle, backend=self._backend,
                                                  on_event=self._lifecycle_event,
                                                  diagnostic_directory=self.directory/'environment')
                self._owner.__enter__()
            self._opened = True
            if self._borrowed is None and hasattr(self._owner, 'initial_observation'):
                self._check_initial(self._owner.initial_observation)
            else:
                self.assert_initial()
            return self
        except BaseException:
            self.close()
            raise

    def assert_initial(self):
        if not self._opened or self._finished:
            raise RuntimeError('Task has not been reset')
        return self._check_initial(self._session.observe())

    def _check_initial(self, observed):
        expected = json.loads((self.bundle.root / 'private/initial.json').read_text())
        if digest(expected) != digest(observed):
            raise RuntimeError('World differs from its packaged initial state')
        return {'matches': True, 'digest': digest(observed), 'observation': observed}

    def reset(self):
        if self._episode or not self._opened or self._borrowed is not None:
            raise RuntimeError('Cannot reset this task now')
        self._owner.reset()
        self._finished = False
        if hasattr(self._owner, 'initial_observation'):
            return self._check_initial(self._owner.initial_observation)
        return self.assert_initial()

    def _start(self, instance_id):
        self._session.start_episode(instance_id)

    def _stop(self):
        self._session.stop_episode()

    def _dispatch(self, name, arguments):
        if name in {'list_resources', 'read_resource', 'query_resource', 'search_resource'}:
            try:
                operation = {'list_resources': self.resources.list, 'read_resource': self.resources.read,
                             'query_resource': self.resources.query, 'search_resource': self.resources.search}[name]
                return operation(**arguments)
            except self._resource_errors as exc:
                return {'error': {'code': exc.code, 'message': exc.message}}
        return self._session.agent_call(name, arguments)

    def observe(self):
        return self._session.observe()

    def evaluate(self):
        self._require_evaluation()
        return self._session.evaluate()

    def observe_and_evaluate(self):
        self._require_evaluation()
        if hasattr(self._session, 'observe_and_evaluate'):
            return self._session.observe_and_evaluate()
        return {'observation': self.observe(), 'evaluation': self.evaluate()}

    def export_receipts(self, destination):
        self._session.export_receipts(Path(destination) / 'native_calls.sqlite')

    def _lifecycle_event(self, value):
        from mac_bench.runtime.common.artifacts import write_json
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'lifecycle.jsonl').open('a') as stream:
            stream.write(json.dumps(value) + '\n')
        write_json({'id': value['sandbox_id'], 'status': 'terminated' if value['closed'] else 'running'},
                   self.directory / 'sandbox.json')

    def _close(self):
        if self._borrowed is not None:
            self._borrowed.stop_episode()
        if self._borrowed is None and self._owner is not None:
            self._owner.close()
