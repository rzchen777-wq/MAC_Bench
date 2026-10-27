"""Common lifecycle guard; revocation precedes capture and evaluation."""
from ..tools import AgentEnvironment


class OwnedTask:
    def _configure(self, view):
        self.view = view
        self.environment = AgentEnvironment(view.tools, self._dispatch)
        self._opened = self._episode = False
        self._finished = False

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
            # A failed capture/stop must never make the candidate gradable.
            self._stop()
            self._episode = False
            self._finished = True

    def _require_evaluation(self):
        if not self._opened or self._episode or not self._finished:
            raise RuntimeError('Evaluation requires a stopped episode')

    def close(self):
        try:
            self.environment.end()
        finally:
            try:
                self._close()
            finally:
                self._opened = self._episode = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
