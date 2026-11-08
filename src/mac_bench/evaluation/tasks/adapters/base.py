"""Common lifecycle guard; revocation precedes capture and evaluation."""
from pathlib import Path
import shutil
from ..tools import AgentEnvironment


class OwnedTask:
    def _configure(self, view):
        self.view = view
        self.environment = AgentEnvironment(view.tools, self._dispatch)
        self._opened = self._episode = False
        self._finished = False
        self._execution = None

    def open_execution(self):
        """Shared public workspace for methods with native file/process tools."""
        if not self._episode:
            raise RuntimeError('Execution requires an active episode')
        if self._execution is None:
            self._execution = self._open_execution()
        return self._execution

    def _close_execution(self):
        if self._execution is not None:
            self._execution.close()
            self._execution = None

    @property
    def is_open(self):
        return self._opened

    def begin_episode(self, instance_id, *, admit_call=lambda: None, event=lambda value: None):
        if not self._opened or self._episode:
            raise RuntimeError('Task is not ready for an episode')
        if self._finished:
            raise RuntimeError('Reset is required between episodes')
        self.assert_initial()
        self._start(instance_id)
        self.environment.begin(admit_call=admit_call, event=event)
        self._episode = True

    def end_episode(self):
        self.environment.end()
        if self._episode:
            self._close_execution()
            # A failed capture/stop must never make the candidate gradable.
            self._stop()
            self._episode = False
            self._finished = True

    def _require_evaluation(self):
        if not self._opened or self._episode or not self._finished:
            raise RuntimeError('Evaluation requires a stopped episode')

    def export_receipts(self, destination):
        archive = Path(self.directory) / 'execution_outputs.tar'
        if archive.exists():
            destination = Path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, destination / archive.name)

    def close(self):
        try:
            self.environment.end()
        finally:
            try:
                try:
                    self._close_execution()
                finally:
                    self._close()
            finally:
                self._opened = self._episode = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
