"""Delegation-aware rolling memory over the original worker record format."""
import json
from mac_bench.runtime.agents.context import TokenEstimate
from mac_bench.runtime.llm.client import ChatMessage


def worker_memory(memory_type, *, llm, instruction, context, template):
    class DelegationMemory(memory_type):
        async def _compress(self):
            size = TokenEstimate().count([ChatMessage('user', json.dumps(self._records,
                ensure_ascii=False, default=str))])
            if len(self._records) < self.max_memory and size < 40000:
                return
            head = self._records[:-self.keep_recent] if self.keep_recent else self._records
            tail = self._records[-self.keep_recent:] if self.keep_recent else []
            if head:
                # Rewrite one persistent state, rather than append stale plans forever.
                self._summary = await self._summarize_records(head)
            self._records = tail

        async def _summarize_records(self, records):
            prompt = template.format(
                instruction=instruction, context=context or 'None',
                previous=self._summary or 'None',
                records=json.dumps([{k: r.get(k) for k in
                    ('observation', 'action', 'thinking', 'reward')} for r in records],
                    ensure_ascii=False, default=str))
            return await self.llm(prompt, max_tokens=12288)

    return DelegationMemory(llm=llm, max_memory=10, keep_recent=3)
