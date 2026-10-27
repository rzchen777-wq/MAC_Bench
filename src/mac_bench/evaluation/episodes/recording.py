"""Thread-safe request/response persistence at the shared model gateway boundary."""
from pathlib import Path
from threading import Lock
from datetime import datetime, timezone
import time
from mac_bench.runtime.common.artifacts import write_json
from mac_bench.runtime.llm.client import ChatResponse, assistant_chat_message
from .identity import call_identity


class RecordingClient:
    def __init__(self, client, directory, *, budget=None):
        self.client, self.budget = client, budget
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        self.count = self.completed_calls = self.input_tokens = self.output_tokens = 0
        self._lock = Lock()

    @property
    def config(self):
        return getattr(self.client, 'config', None)

    @property
    def capabilities(self):
        return getattr(self.client, 'capabilities', frozenset())

    def complete(self, messages, **kwargs):
        from uuid import uuid4
        from mac_bench.runtime.llm.gateway import is_retryable
        from .identity import call_scope

        # Provider retries stay outside LiteLLM so every attempted request has
        # its own receipt and consumes the shared call/wall budget.
        logical_id = call_identity().get('logical_call_id') or uuid4().hex
        for attempt in range(3):
            with call_scope(logical_call_id=logical_id,
                            provider_attempt_id=f'{logical_id}:transport:{attempt + 1}'):
                try:
                    return self._complete_once(messages, **kwargs)
                except Exception as exc:
                    if (attempt == 2 or not is_retryable(exc)
                            or isinstance(getattr(exc, 'response', None), ChatResponse)):
                        raise
            time.sleep(2 ** attempt)

    def _complete_once(self, messages, **kwargs):
        if self.budget:
            self.budget.admit_model()
        with self._lock:
            self.count += 1
            path = self.root / f'{self.count:04}.json'
        record = {'status': 'pending', 'messages': [m.as_dict() for m in messages],
                  'identity': call_identity(), **kwargs}
        started = time.monotonic()
        record['started_at_utc'] = datetime.now(timezone.utc).isoformat()
        try:
            write_json(record, path)
            response = self.client.complete(messages, **kwargs)
            if self.budget:
                self.budget.complete_model(response.usage)
            with self._lock:
                self.completed_calls += 1
                self.input_tokens += int(response.usage.get('prompt_tokens', 0))
                self.output_tokens += int(response.usage.get('completion_tokens', 0))
            record.update(status='completed', response=assistant_chat_message(response).as_dict(),
                model=response.model, usage=dict(response.usage), finish_reason=response.finish_reason)
            record.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                          elapsed_seconds=time.monotonic() - started)
            write_json(record, path)
            return response
        except BaseException as exc:
            record.update(status='error', error_type=type(exc).__name__)
            record.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                          elapsed_seconds=time.monotonic() - started)
            partial = getattr(exc, 'response', None)
            if isinstance(partial, ChatResponse):
                # A rejected/truncated completion still incurred provider usage.
                if self.budget:
                    self.budget.complete_model(partial.usage)
                with self._lock:
                    self.input_tokens += int(partial.usage.get('prompt_tokens', 0))
                    self.output_tokens += int(partial.usage.get('completion_tokens', 0))
                record.update(response=assistant_chat_message(partial).as_dict(),
                              model=partial.model, usage=dict(partial.usage),
                              finish_reason=partial.finish_reason, response_incomplete=True)
            write_json(record, path)
            raise
        finally:
            if self.budget:
                self.budget.release_model()
