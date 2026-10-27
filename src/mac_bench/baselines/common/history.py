"""Bound a native agent's API view without changing its planning or tool loop."""
import hashlib
import json
from mac_bench.runtime.agents.context import WorkingContext
from mac_bench.runtime.llm.client import ChatMessage, ModelOutputTruncatedError
from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.baselines.common.gateway import MethodStop


class NativeHistory:
    def __init__(self, gateway, policy):
        self.gateway = gateway
        self.working = WorkingContext(policy)
        self.keys, self.messages = [], []

    def sync(self, native):
        converted = [m if isinstance(m, ChatMessage) else ChatMessage(**{
            k: v for k, v in m.items() if k in ChatMessage.__dataclass_fields__}) for m in native]
        keys = [hashlib.sha256(json.dumps(m.as_dict(), sort_keys=True).encode()).hexdigest()
                for m in converted]
        if self.keys and keys[:len(self.keys)] == self.keys:
            self.messages.extend(converted[len(self.keys):])
        else:
            # The upstream agent has reset/trimmed/replaced its own history.
            self.messages = converted
        self.keys = keys
        return self.messages

    def prepare(self, tools, cap):
        def summarize(batch, allowance):
            with call_scope(phase='context_summary'):
                try:
                    return self.gateway.complete(batch, max_tokens=allowance,
                        temperature=0, recover_truncation=False)
                except MethodStop as stop:
                    if isinstance(stop.error, ModelOutputTruncatedError):
                        raise stop.error
                    raise
        event = self.working.prepare(self.messages, tools, cap, summarize, self.gateway.context.event)
        if event:
            self.gateway.context.event(event)
        return event

    def commit(self, native):
        # Keep the API-view list object stable across larger-cap retries.
        native[:] = [m.as_dict() for m in self.messages]
        self.keys = [hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()
                     for m in native]
