"""AutoGen protocol conversion and bounded, recorded model calls."""
import hashlib
import json
from autogen_core import FunctionCall
from autogen_core.models import ChatCompletionClient, CreateResult, RequestUsage
from autogen_core.tools import BaseTool
from pydantic import BaseModel
from mac_bench.runtime.agents.context import WorkingContext
from mac_bench.runtime.llm.client import ChatMessage, ModelOutputTruncatedError
from mac_bench.evaluation.episodes.identity import call_scope
from mac_bench.baselines.common.gateway import MethodStop, agent_scope


def convert(messages):
    result = []
    for m in messages:
        if m.type == 'SystemMessage':
            result.append(ChatMessage('system', m.content))
        elif m.type == 'UserMessage':
            if not isinstance(m.content, str):
                raise ValueError('This benchmark adapter supports text tools, not images')
            result.append(ChatMessage('user', f'[{m.source}]\n{m.content}'))
        elif m.type == 'AssistantMessage':
            if isinstance(m.content, str):
                result.append(ChatMessage('assistant', m.content))
            else:
                result.append(ChatMessage('assistant', m.thought or '', tool_calls=tuple(
                    {'id':c.id,'type':'function','function':{'name':c.name,'arguments':c.arguments}}
                    for c in m.content)))
        elif m.type == 'FunctionExecutionResultMessage':
            result.extend(ChatMessage('tool', x.content, tool_call_id=x.call_id) for x in m.content)
        else:
            raise ValueError('Unsupported native message: '+m.type)
    return result


class RecordedModel(ChatCompletionClient):
    def __init__(self, context, gateway, *, name, role, policy, failures):
        self.context, self.gateway = context, gateway
        self.name, self.role = name, role
        self.policy, self.failures = policy, failures
        self.working = WorkingContext(policy)
        self.keys, self.messages = [], []
        self.ledger, self.request = '', ''
        self.usage = RequestUsage(0, 0)
        context.event({'kind':'context_policy','agent_id':name,'policy':vars(policy)})

    @property
    def model_info(self):
        return {'vision':False,'function_calling':True,'json_output':False,
                'structured_output':False,'family':'unknown','multiple_system_messages':True}

    @property
    def capabilities(self):
        return self.model_info

    def count_tokens(self, messages, *, tools=()):
        return self.working.tokens.count(convert(messages), self._tools(tools))

    def remaining_tokens(self, messages, *, tools=()):
        return max(0, self.policy.request_token_limit-self.count_tokens(messages,tools=tools))

    def actual_usage(self):
        return self.usage

    def total_usage(self):
        return self.usage

    async def close(self):
        pass

    @staticmethod
    def _tools(tools):
        return [{'type':'function','function':{
            'name':s['name'],'description':s.get('description',''),
            'parameters':s.get('parameters',{'type':'object','properties':{}})}}
            for t in tools for s in [t.schema if hasattr(t,'schema') else t]]

    def _sync(self, native):
        converted = convert(native)
        for m in converted:
            if isinstance(m.content,str) and 'We are working to address the following user request:' in m.content:
                self.ledger=m.content
        requests=[m.content for m in converted if m.role=='user' and
                  (self.role=='orchestrator' or str(m.content).startswith(
                      ('[Orchestrator]\n','[MagenticOneOrchestrator]\n')))]
        self.request=requests[-1] if requests else ''
        # The coordinator appends a temporary ledger question to each API
        # request, but does not commit it to its message thread. Pin that
        # question above; exclude it from the incremental history identity.
        if self.role=='orchestrator' and converted and converted[-1].role=='user':
            converted=converted[:-1]
        keys = [hashlib.sha256(json.dumps(m.as_dict(),sort_keys=True).encode()).hexdigest()
                for m in converted]
        if self.keys and keys[:len(self.keys)] == self.keys:
            self.messages.extend(converted[len(self.keys):])
        else:
            rules = '\n\n'.join(m.content for m in converted if m.role == 'system')
            self.messages = [ChatMessage('system', rules or 'Follow the supplied team instructions.'),
                ChatMessage('user', self.context.task.instruction+'\n\n'+self.context.task.interaction_instructions)]
            self.messages.extend(m for m in converted if m.role != 'system')
        self.keys = keys

    async def create(self, messages, *, tools=(), tool_choice='auto', json_output=None,
                     extra_create_args=None, cancellation_token=None):
        if self.failures:
            raise RuntimeError('Episode model/tool gateway has stopped')
        if tool_choice not in ('auto','none') or json_output:
            raise ValueError('Unsupported forced tool/structured output mode')
        if cancellation_token and cancellation_token.is_cancelled():
            raise RuntimeError('Native model call cancelled before admission')
        schemas = self._tools(tools) if tool_choice == 'auto' else []
        self._sync(messages)
        phase = 'orchestration' if self.role == 'orchestrator' else 'execution'
        async def call(batch, **kwargs):
            response = await self.gateway.async_complete(batch, **kwargs)
            self.usage.prompt_tokens += response.usage.get('prompt_tokens',0)
            self.usage.completion_tokens += response.usage.get('completion_tokens',0)
            return response
        # WorkingContext is synchronous; execute its helper calls on a thread via
        # the same recorded gateway. Native execution is non-streaming.
        def summarize(batch, allowance):
            with call_scope(phase='context_summary'):
                try:
                    response = self.gateway.complete(batch,max_tokens=allowance,temperature=0,
                        recover_truncation=False)
                except MethodStop as stop:
                    if isinstance(stop.error,ModelOutputTruncatedError):
                        raise stop.error
                    raise
                self.usage.prompt_tokens += response.usage.get('prompt_tokens',0)
                self.usage.completion_tokens += response.usage.get('completion_tokens',0)
                self.context.event({'kind':'context_summary','agent_id':self.name,'usage':dict(response.usage)})
                return response
        try:
            with agent_scope(self.context,self.name,self.role,phase):
                state=self.context.budget.snapshot()
                remaining=max(0,self.context.budget.limits.max_model_calls-state['model_call_attempts'])
                self.messages[1]=ChatMessage('user',self.context.task.instruction+'\n\n'+
                    self.context.task.interaction_instructions+
                    '\n\nCURRENT TASK LEDGER (plan, not verified completion):\n'+self.ledger+
                    '\n\nCURRENT COORDINATION REQUEST:\n'+self.request+
                    '\n\nSHARED BUDGET: '+str(remaining)+
                    ' model calls remain before this turn, including coordination and summaries. '
                    'Report actual progress and unresolved work when resources are low.')
                import asyncio
                batch = list(self.messages)
                # Native ledger prompts are temporary API messages. Keep the
                # latest question LAST, as upstream does; pinning it before old
                # conversation made the coordinator answer stale worker turns.
                temporary_request = self.role == 'orchestrator' and bool(self.request)
                if temporary_request:
                    batch.append(ChatMessage('user',self.request))
                pending=asyncio.create_task(asyncio.to_thread(self.working.prepare,batch,schemas,
                    self.gateway.parameters['max_tokens'],summarize,self.context.event))
                try:
                    compaction=await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await pending
                    raise
                if compaction:
                    self.context.event({'agent_id':self.name,**compaction})
                calibration_batch = batch
                def prepare_recovery(cap, recovery_batch=None):
                    nonlocal calibration_batch
                    calibration_batch = batch if recovery_batch is None else recovery_batch
                    event = self.working.prepare(calibration_batch,
                        schemas,cap,summarize,self.context.event)
                    if event:
                        self.context.event({'agent_id':self.name,**event})
                response=await call(batch,tools=schemas,prepare_context_batch=prepare_recovery,
                                   continue_text=self.name == 'Coder' and not schemas)
                self.messages[:] = batch[:-1] if temporary_request else batch
                self.working.tokens.observe(calibration_batch,schemas,response.usage)
        except MethodStop as stop:
            # AutoGen reports ordinary exceptions through its runtime queue.
            # Retain the original exception for the common episode evaluator.
            self.failures.append(stop.error)
            raise RuntimeError('Recorded gateway stopped: '+type(stop.error).__name__) from None
        content=response.content
        if tool_choice == 'none' and response.tool_calls:
            raise ValueError('Native tool-free reflection returned tool calls')
        if response.tool_calls:
            content=[FunctionCall(id=c['id'],name=c['function']['name'],arguments=c['function']['arguments'])
                     for c in response.tool_calls]
        return CreateResult(content=content,finish_reason='function_calls' if response.tool_calls else 'stop',
            usage=RequestUsage(response.usage.get('prompt_tokens',0),response.usage.get('completion_tokens',0)),
            cached=False,thought=response.content if response.tool_calls and response.content else None)

    async def create_stream(self, messages, **kwargs):
        yield await self.create(messages, **kwargs)


class WorldTool(BaseTool):
    def __init__(self, definition, gateway, name, lock, failures):
        super().__init__(BaseModel, dict, definition.name, definition.description)
        self.definition,self.gateway,self.worker=definition,gateway,name
        self.lock,self.failures=lock,failures

    @property
    def schema(self):
        return {'name':self.definition.name,'description':self.definition.description,
                'parameters':self.definition.input_schema}

    def return_value_as_string(self,value):
        return json.dumps(value,ensure_ascii=False)

    async def run(self,args,cancellation_token):
        return await self.run_json(args.model_dump(),cancellation_token)

    async def run_json(self,args,cancellation_token,call_id=None):
        if self.failures:
            raise RuntimeError('Episode gateway stopped')
        async with self.lock:
            with agent_scope(self.gateway.context,self.worker,'executor'):
                try:
                    return await self.gateway.tool(self.definition.name,dict(args))
                except MethodStop as stop:
                    self.failures.append(stop.error)
                    raise RuntimeError('Recorded tool gateway stopped') from None
