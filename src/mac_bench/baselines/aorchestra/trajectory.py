"""Native delegation report from correctly paired action outcomes."""
import asyncio
import json
from mac_bench.runtime.agents.context import ContextPolicy, WorkingContext
from mac_bench.runtime.llm.client import ChatMessage, ModelOutputTruncatedError
from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.baselines.common.gateway import MethodStop


def report_messages(trace, instruction):
    messages = [ChatMessage('system',
        'You summarize one delegated worker trajectory for its coordinator. '
        'Report observed progress, actual changes and checks, unresolved work, and the '
        'worker final report. Do not solve the task or invent results. Each action below '
        'is paired with its own outcome after execution, not its preceding observation. '
        'A successful paginated read is not a failure merely because it contains one page. '
        'Preserve next_offset/next_cursor, IDs, paths and completion evidence when useful. '
        'Distinguish reading the requirements from implementing them. Return a useful '
        'English handover summary; no fixed line or bullet count.'),
        ChatMessage('user', 'Delegated assignment:\n'+instruction)]
    for step in trace:
        messages.append(ChatMessage('assistant', json.dumps(step.action, ensure_ascii=False, default=str)))
        info = step.info or {}
        outcome = info.get('finish_result') if info.get('finished') else info.get('last_action_result')
        messages.append(ChatMessage('user', json.dumps({'outcome_after_action': outcome,
            'finished': bool(info.get('finished')), 'reward': step.reward}, ensure_ascii=False, default=str)))
    return messages


async def summarize_trace(gateway, trace, instruction):
    if not trace:
        return 'No steps executed.'
    messages = report_messages(trace, instruction)
    working = WorkingContext(ContextPolicy())
    def summarize(batch, allowance):
        with call_scope(phase='context_summary'):
            try:
                return gateway.complete(batch, max_tokens=allowance,
                    temperature=0, recover_truncation=False)
            except MethodStop as stop:
                if isinstance(stop.error, ModelOutputTruncatedError):
                    raise stop.error
                raise
    def prepare(cap):
        event = working.prepare(messages, (), cap, summarize, gateway.context.event)
        if event:
            gateway.context.event(event)
    # Gateway executes preflight/helpers on its shielded request thread.
    return (await gateway.async_complete(messages, max_tokens=12288,
        prepare_context=prepare)).content.strip()
