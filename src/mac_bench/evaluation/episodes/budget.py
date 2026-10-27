"""One thread-safe admission/accounting budget for every worker in an episode."""
import math
import time
from threading import RLock, Condition
from mac_bench.runtime.agents.workflow import AgentBudget


class BudgetExceeded(RuntimeError):
    pass


class EpisodeBudget:
    def __init__(self, limits=None):
        self.limits = limits or AgentBudget(max_model_calls=200, max_tool_calls=200,
            max_env_actions=200, max_tokens=1000000, max_wall_seconds=2400)
        for name in ('max_model_calls', 'max_tool_calls', 'max_env_actions', 'max_tokens'):
            value = getattr(self.limits, name)
            if type(value) is not int or value < 0:
                raise ValueError('Invalid episode budget: ' + name)
        seconds = self.limits.max_wall_seconds
        if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
            raise ValueError('Invalid wall-clock budget')
        self.started = time.monotonic()
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._accepting = True
        self._model_inflight = 0
        self._ended = None
        self.model_call_attempts = self.model_calls = self.tool_calls = 0
        self.input_tokens = self.output_tokens = 0

    def _check(self):
        if not self._accepting:
            raise BudgetExceeded("episode_closed")
        if self.limits.max_wall_seconds is not None and time.monotonic() - self.started >= self.limits.max_wall_seconds:
            raise BudgetExceeded('wall_seconds')
        if self.input_tokens + self.output_tokens >= self.limits.max_tokens:
            raise BudgetExceeded('tokens')

    def admit_model(self):
        with self._lock:
            self._check()
            if self.model_call_attempts >= self.limits.max_model_calls:
                raise BudgetExceeded('model_calls')
            self.model_call_attempts += 1
            self._model_inflight += 1

    def admit_tool(self):
        with self._lock:
            self._check()
            if self.tool_calls >= min(self.limits.max_tool_calls, self.limits.max_env_actions):
                raise BudgetExceeded('tool_calls')
            self.tool_calls += 1

    def complete_model(self, usage):
        with self._lock:
            prompt, completion = usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0)
            if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
                raise ValueError('Invalid provider token usage')
            self.model_calls += 1
            self.input_tokens += prompt
            self.output_tokens += completion

    def release_model(self):
        with self._condition:
            self._model_inflight -= 1
            self._condition.notify_all()

    def finish(self):
        with self._condition:
            self._accepting = False
            while self._model_inflight:
                self._condition.wait()
            if self._ended is None:
                self._ended = time.monotonic()

    def snapshot(self):
        with self._lock:
            return {'model_calls': self.model_calls, 'model_call_attempts': self.model_call_attempts,
                'tool_calls': self.tool_calls, 'input_tokens': self.input_tokens,
                'output_tokens': self.output_tokens,
                'token_limit_exceeded': self.input_tokens + self.output_tokens > self.limits.max_tokens,
                'wall_limit_exceeded': self.limits.max_wall_seconds is not None and
                    (self._ended or time.monotonic()) - self.started > self.limits.max_wall_seconds}
