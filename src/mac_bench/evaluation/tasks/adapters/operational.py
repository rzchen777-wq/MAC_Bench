"""Adapt the real native world, keeping owner RPC and grading out of agent tools."""
import json
from copy import deepcopy
from pathlib import Path
from mac_bench.runtime.common.hashing import digest
from mac_bench.environments.packaging.bundle import WorldBundle
from mac_bench.environments.tools.api import SCHEMAS, DESCRIPTIONS, INTERFACE_VERSION
from ..protocol import TaskView, ToolDefinition
from .base import OwnedTask
from ..resources import PublicResources, LIST_RESOURCES, READ_RESOURCE, QUERY_RESOURCE, SEARCH_RESOURCE
from mac_bench.environments.tools.contracts import ToolError


class OperationalTask(OwnedTask):
    def __init__(self, record, directory, *, backend=None, owner=None, bundle=None, session=None):
        self.record, self.directory = record, Path(directory)
        self.bundle = bundle or WorldBundle.load(record.bundle)
        if self.bundle.manifest.get('metadata', {}).get('tool_api_version') != INTERFACE_VERSION:
            raise ValueError('Operational tasks require tool API v2')
        self._owner, self._borrowed, self._backend = owner, session, backend
        self._scratch = None
        self.resources = PublicResources(self.bundle)
        self._resource_errors = (ToolError,)
        instructions = (self.bundle.root / 'private/agent_instructions.txt').read_text()
        self._configure(TaskView(record.task_id, record.world_revision, self.resources.instruction(),
                          (LIST_RESOURCES, READ_RESOURCE, QUERY_RESOURCE, SEARCH_RESOURCE) + tuple(
                              ToolDefinition(k, DESCRIPTIONS[k], deepcopy(v)) for k, v in SCHEMAS.items()), instructions))

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
        (self.directory / 'execution_outputs.tar').unlink(missing_ok=True)
        self._session.start_episode(instance_id)

    def _open_execution(self):
        from ..execution import TaskExecution
        from mac_bench.runtime.sandbox.modal import ModalSandboxBackend
        # Team notes and scripts share one small workspace, separate from the
        # application host. Application access still goes through public tools.
        backend = ModalSandboxBackend(cpu=.5, memory_mib=512, timeout=86400)
        sandbox = backend.create(name='mac-bench-agent-workspace', image='python:3.12-slim',
                                 network_blocked=True)
        self._scratch = (backend, sandbox)
        self._workspace_receipt(sandbox, 'running')
        try:
            result = backend.exec(sandbox, 'mkdir -p /work/project /work/tool-output', timeout=30)
            if result.exit_code:
                raise RuntimeError('Cannot prepare the shared agent workspace')
            backend.write_files(sandbox, files={
                '/work/project/' + name: (self.bundle.root / name).read_bytes()
                for name in self.resources.names})
            return TaskExecution(backend, sandbox, '/work/project',
                                 output_archive=self.directory / 'execution_outputs.tar',
                                 public_environment=self.environment, tool_definitions=self.view.tools)
        except BaseException:
            self._close_scratch()
            raise

    def _close_scratch(self):
        if self._scratch is not None:
            backend, sandbox = self._scratch
            backend.delete(sandbox)
            self._workspace_receipt(sandbox, 'terminated')
            self._scratch = None

    def _workspace_receipt(self, sandbox, status):
        from mac_bench.runtime.common.artifacts import write_json
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json({'id': sandbox.id, 'status': status}, self.directory / 'agent_workspace.json')

    def _close_execution(self):
        try:
            super()._close_execution()
        finally:
            self._close_scratch()

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
        return self._session.observe_and_evaluate()

    def export_receipts(self, destination):
        super().export_receipts(destination)
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
