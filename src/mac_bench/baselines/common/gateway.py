"""Native response bridges; all generative work crosses the recorded gateway."""
import asyncio
from contextlib import contextmanager
from uuid import uuid4
from mac_bench.runtime.llm.client import ChatMessage, assistant_chat_message, ModelOutputTruncatedError
from mac_bench.evaluation.episodes.identity import call_scope, call_identity
from mac_bench.evaluation.episodes.budget import BudgetExceeded


class MethodStop(BaseException):
    """Escape native broad Exception handlers; translated at method boundary."""
    def __init__(self, error):
        self.error = error


class Gateway:
    def __init__(self, context, parameters):
        self.context, self.parameters = context, parameters

    def complete(self, messages, **kwargs):
        # Regenerate a complete response; native callers may be decoding JSON,
        # XML actions or tool calls. Never deliver/execute a truncated draft.
        recover = kwargs.pop('recover_truncation', True)
        prepare = kwargs.pop('prepare_context', None)
        prepare_batch = kwargs.pop('prepare_context_batch', None)
        continue_text = kwargs.pop('continue_text', False)
        original_messages = list(messages)
        prefix = ''
        cap = kwargs.get('max_tokens', self.parameters['max_tokens'])
        ceiling = max(cap, self.parameters.get('recovery_max_tokens', 32768))
        attempts = self.parameters.get('recovery_attempts', 2) if recover else 0
        for attempt in range(attempts + 1):
            if prepare_batch:
                prepare_batch(cap, messages)
            elif prepare:
                prepare(cap)
            try:
                response = self._complete_once(messages, **{**kwargs, 'max_tokens': cap})
                if prefix:
                    if response.tool_calls:
                        raise MethodStop(RuntimeError('Text continuation unexpectedly returned function calls'))
                    from dataclasses import replace
                    response = replace(response, content=prefix + response.content)
                return response
            except MethodStop as stop:
                error = stop.error
                if (not isinstance(error, ModelOutputTruncatedError)
                        or error.response is None or attempt == attempts or cap >= ceiling):
                    raise
                cap = min(ceiling, cap * 2)
                if continue_text and not error.response.tool_calls and error.response.content:
                    # The assembled message reaches the native agent only after
                    # a finished response. No interrupted code block is executed.
                    prefix += error.response.content
                    messages = list(original_messages) + [ChatMessage('assistant', prefix),
                        ChatMessage('user', 'Your reply was interrupted before delivery. None of its code '
                            'was executed. Continue ONLY the missing text from the exact end above; '
                            'do not restart or repeat the reply. Finish the current code fragment and '
                            'close any open Markdown fence. Keep this response bounded; later work '
                            'can use later replies with real execution feedback.')]
                    self.context.event({'kind': 'completion_recovery', 'attempt': attempt + 1,
                        'max_output_tokens': cap, 'strategy': 'continue_unexecuted_text',
                        'prefix_characters': len(prefix), **call_identity()})
                    continue
                self.context.event({'kind': 'completion_recovery', 'attempt': attempt + 1,
                    'max_output_tokens': cap, 'strategy': 'regenerate_complete_response',
                    **call_identity()})
                instruction = self.parameters.get('recovery_instruction')
                if instruction:
                    # A larger output cap alone repeats runaway action streams.
                    # The failed response was never executed; request a fresh,
                    # smaller response without treating its draft as history.
                    messages = list(messages) + [ChatMessage('user',
                        'The previous response exceeded its output limit and was rejected. '
                        'No action from that response was executed. ' + instruction)]

    def _complete_once(self, messages, **kwargs):
        logical_id = uuid4().hex
        with call_scope(logical_call_id=logical_id, provider_attempt_id=logical_id + ':1'):
            self.context.event({'kind': 'method_model_start', **call_identity()})
            try:
                options = {'temperature': self.parameters['temperature'],
                           'max_tokens': self.parameters['max_tokens']}
                if 'max_tokens' in kwargs:
                    options['max_tokens'] = kwargs['max_tokens']
                if 'temperature' in kwargs:
                    options['temperature'] = kwargs['temperature']
                if kwargs.get('tools'):
                    options['tools'] = kwargs['tools']
                if kwargs.get('stop'):
                    options['stop'] = kwargs['stop']
                converted = [m if isinstance(m, ChatMessage) else ChatMessage(**{
                    k:v for k,v in m.items() if k in ChatMessage.__dataclass_fields__}) for m in messages]
                response = self.context.client.complete(converted, **options)
            except BudgetExceeded as exc:
                raise MethodStop(exc) from exc
            except Exception as exc:
                # A provider/storage error cannot be silently swallowed and
                # converted to a fabricated native solver result.
                raise MethodStop(exc) from exc
            finally:
                self.context.event({'kind': 'method_model_end', **call_identity()})
            return response

    async def async_complete(self, messages, **kwargs):
        # Await shielded in-flight work on cancellation: a to_thread call cannot
        # be killed, and must finish before recording/capture is finalized.
        pending = asyncio.create_task(asyncio.to_thread(self.complete, messages, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await pending
            except MethodStop:
                pass
            raise

    async def native_complete(self, model, messages, **kwargs):
        expected = self.context.client.config.model
        if model not in {expected, 'experiment.model'}:
            raise ValueError('Native model binding is not the selected backbone')
        if hasattr(self, 'native_contexts'):
            from mac_bench.baselines.common.history import NativeHistory
            identity = call_identity()
            key = (identity.get('agent_id'), identity.get('phase'))
            history = self.native_contexts.setdefault(key, NativeHistory(self, self.context_policy))
            native_messages = messages
            messages = history.sync(messages)
            schemas = kwargs.get('tools') or ()
            def prepare(cap):
                if history.prepare(schemas, cap):
                    # Commit valid memory to the agent's own list, so its native
                    # message-count trimming cannot later erase that memory.
                    history.commit(native_messages)
            kwargs['prepare_context'] = prepare
        response = await self.async_complete(messages, **kwargs)
        if hasattr(self, 'native_contexts'):
            history.working.tokens.observe(messages, schemas, response.usage)
        raw = assistant_chat_message(response).as_dict()
        from litellm import ModelResponse
        usage = dict(response.usage)
        usage['total_tokens'] = usage.get('prompt_tokens',0) + usage.get('completion_tokens',0)
        return ModelResponse(model=response.model, id=response.raw_id or uuid4().hex,
            choices=[{'index':0,'message':raw,
                      'finish_reason':response.finish_reason or ('tool_calls' if response.tool_calls else 'stop')}],
            usage=usage)

    async def tool(self, name, arguments):
        async def invoke():
            try:
                return await asyncio.to_thread(self.context.environment.call, name, arguments)
            except Exception as exc:
                raise MethodStop(exc) from exc
        pending = asyncio.create_task(invoke())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await pending
            except MethodStop:
                pass
            raise


@contextmanager
def agent_scope(context, agent_id, role, phase='execution'):
    with call_scope(agent_id=agent_id, role=role, phase=phase):
        context.event({'kind':'agent_start','agent_id':agent_id,'role':role})
        try:
            yield
        finally:
            context.event({'kind':'agent_end','agent_id':agent_id,'role':role})
