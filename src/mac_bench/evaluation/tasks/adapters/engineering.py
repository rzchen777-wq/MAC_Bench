"""Persistent terminal workspace, frozen source submission, independent grader."""
from pathlib import Path
from hashlib import sha256
from threading import Lock
from mac_bench.environments.packaging.bundle import WorldBundle
from ..protocol import TaskView, ToolDefinition
from .base import OwnedTask
from ..terminal_output import capture_command, output_result

TERMINAL = ToolDefinition('terminal',
    'Run a shell command in /work/project. Edit source and run public checks in the persistent workspace. '
    'Use the interpreter and dependencies described in the task. Full stdout/stderr is saved at '
    'full_output_path; large output returns a bounded preview. Use grep or selected line ranges on that '
    'file instead of rerunning the command or printing the entire log. Nonzero exit codes are returned. '
    'Timeout stops the command process group. Each command and its descendants share a 6 GiB resident-memory cap; '
    'concurrent commands share a 6 GiB resident-memory cap, stopping the largest active command if exceeded. '
    'Individual processes also have a 6 GiB data-allocation cap. Workspace files persist after limits.',
    {'type': 'object', 'properties': {'command': {'type': 'string', 'minLength': 1},
        'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 300}},
     'required': ['command'], 'additionalProperties': False})
GUIDE = '''Complete the code task by editing the actual workspace through terminal.
Read /work/TASK.md for the full requirements and public materials before making changes.
Your working directory is /work/project. Read the task for interpreter and test
commands; use public tests to check progress. Changes persist within this episode.
The current source tree is your submission. A prose answer alone does not solve
the task. The workspace has no external network access; use its provided source,
documentation, tests and preinstalled dependencies. Local services remain usable.
Hidden grading runs separately and is not an available tool.'''


class EngineeringTask(OwnedTask):
    def __init__(self, record, directory, *, backend=None, world=None):
        self.record, self.directory = record, Path(directory)
        self.bundle = WorldBundle.load(record.bundle)
        self._world, self._backend = world, backend
        self._submission = None
        self._output_number = 0
        self._output_lock = Lock()
        instruction = ('Complete the software task documented in /work/TASK.md. '
                       'Read that file through terminal for the full requirements, public materials, '
                       'interpreter and test commands. The persistent source workspace is /work/project. '
                       'Inspect its source, documentation and tests as needed; implement and check your changes there.')
        self._configure(TaskView(record.task_id, record.world_revision, instruction, (TERMINAL,), GUIDE))

    def open(self):
        if self._opened:
            raise RuntimeError('Task is already open')
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self._world is None:
                if self.record.category == 'product_application':
                    from mac_bench.environments.engineering.product.runtime import LoadedProductWorld as World
                else:
                    from mac_bench.environments.engineering.code_world import EngineeringCodeWorld as World
                self._world = World(self.record.bundle, self.directory, backend=self._backend)
            self._world.open()
            self._opened = True
            return self
        except BaseException:
            self.close()
            raise

    def assert_initial(self):
        if not self._opened or self._episode or self._finished or self._submission is not None:
            raise RuntimeError('Task has not been reset')
        # The underlying loader installs the hashed starter in a fresh sandbox.
        return {'matches': True, 'world_revision': self.record.world_revision}

    def reset(self):
        if self._episode or not self._opened:
            raise RuntimeError('Cannot reset this task now')
        self._world.reset()
        self._submission = None
        self._finished = False
        return self.assert_initial()

    def _start(self, instance_id):
        (self.directory / 'execution_outputs.tar').unlink(missing_ok=True)
        self._instance = instance_id
        self._output_number = 0

    def _open_execution(self):
        from ..execution import TaskExecution
        runtime = self._world.runtime
        return TaskExecution(runtime.backend, runtime.sandbox, '/work/project',
                             output_archive=self.directory / 'execution_outputs.tar')

    def _stop(self):
        target = self.directory / ('submission-' + sha256(self._instance.encode()).hexdigest()[:16] + '.tar')
        self._submission = self._world.export_submission(target)

    def _dispatch(self, name, arguments):
        with self._output_lock:
            self._output_number += 1
            output_number = self._output_number
        episode = sha256(self._instance.encode()).hexdigest()[:16]
        log_path = f'/work/tool-output/{episode}/{output_number:06}.log'
        timeout = arguments.get('timeout', 120)
        command = capture_command(arguments['command'], log_path, timeout_seconds=timeout)
        # Allow the supervisor to stop descendants and return its saved log.
        result = self._world.execute(command, timeout=timeout + 15)
        return output_result(result, log_path)

    def observe(self):
        if self._episode:
            raise RuntimeError('Stop the episode before capturing source state')
        if self._submission is None:
            return {'world_revision': self.record.world_revision, 'state': 'initial'}
        return {'submission_sha256': sha256(self._submission.read_bytes()).hexdigest()}

    def evaluate(self):
        self._require_evaluation()
        return self._world.grade_submission(self._submission)

    def export_receipts(self, destination):
        super().export_receipts(destination)
        # Keep full terminal logs after sandbox deletion, including output that
        # the model only saw through a bounded preview. Owner-side export is not
        # an agent tool call and does not expose private grader materials.
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        runtime = getattr(self._world, 'runtime', None)
        if runtime is None or not self._output_number:
            return
        episode = sha256(self._instance.encode()).hexdigest()[:16]
        result = runtime.command('export-tool-output',
            f'tar cf /work/episode-tool-output.tar -C /work/tool-output {episode}')
        if result.exit_code:
            raise RuntimeError('Cannot archive full terminal outputs')
        (destination / 'terminal_outputs.tar').write_bytes(
            runtime.read_archive('/work/episode-tool-output.tar'))

    def _close(self):
        if self._world is not None:
            self._world.close()
