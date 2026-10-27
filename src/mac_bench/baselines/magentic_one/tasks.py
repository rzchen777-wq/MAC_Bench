"""Official ledger orchestration with domain-adapted shared-world workers."""
import asyncio
import importlib.metadata
import json
import logging
import ast
import sys
from mac_bench.baselines.common.config import ROOT, load_spec, resolve, tree_hash
from mac_bench.baselines.common.gateway import Gateway
from mac_bench.baselines.common.source import source_modules
from mac_bench.runtime.agents.context import ContextPolicy
from mac_bench.baselines.magentic_one.orchestration import repaired_orchestrator


class MagenticOne:
    name = 'magentic_one'

    def __init__(self, config=None):
        self.spec=load_spec(self.name,config)

    def validate(self):
        for package in ('autogen-core','autogen-agentchat','autogen-ext'):
            if importlib.metadata.version(package) != '0.7.5':
                raise ValueError('Install the magentic_one extra with pinned AutoGen 0.7.5')
        ContextPolicy(**self.spec['context_policy'])
        if self.spec['limits']['max_turns'] < 1 or self.spec['limits']['max_stalls'] < 1:
            raise ValueError('Magentic-One limits must be positive')
        if [m['name'] for m in self.spec['teams']['engineering']] != ['FileSurfer','Coder','ComputerTerminal']:
            raise ValueError('Engineering uses the native FileSurfer/Coder/ComputerTerminal team')
        if not self.spec['executor']['python'] or self.spec['executor']['timeout'] <= 0:
            raise ValueError('Provide a candidate Python executable and positive execution timeout')
        if self.spec['execution']['worker_tool_iterations_per_turn'] < 1:
            raise ValueError('Application tool iteration limit must be positive')
        for domain in ('operational',):
            members=self.spec['teams'][domain]
            if not members or len({m['name'] for m in members})!=len(members):
                raise ValueError('Domain team names must be nonempty and unique')
            for member in members:
                (ROOT/'src/mac_bench/baselines/magentic_one/prompts'/member['prompt']).read_text()

    def run(self,context):
        self.validate()
        domain='engineering' if {t.name for t in context.task.tools}=={'terminal'} else 'operational'
        root=ROOT/self.spec['source']['directory']/'python/packages'
        directory=resolve(self.spec,context,assets={'domain':domain,
            'adaptation':('native_coder_executor_remote_file_surfer' if domain == 'engineering'
                          else 'native_ledgers_application_api_workers'),
            'prompts':tree_hash(ROOT/'src/mac_bench/baselines/magentic_one/prompts')})
        with source_modules(root/'autogen-agentchat/src',
                            ('autogen_core','autogen_agentchat','autogen_ext',
                             'mac_bench.baselines.magentic_one.bridge','mac_bench.baselines.magentic_one.world_agents')):
            sys.path.insert(0,str(root/'autogen-core/src'))
            sys.path.insert(0,str(root/'autogen-ext/src'))
            with repaired_orchestrator(engineering=domain == 'engineering', event=context.event):
                return asyncio.run(self._run(context,directory,domain))

    async def _run(self,context,directory,domain):
        from autogen_agentchat.agents import AssistantAgent
        from autogen_agentchat.teams import MagenticOneGroupChat
        from .bridge import RecordedModel, WorldTool
        gateway=Gateway(context,self.spec['model_parameters'])
        failures=[]
        lock=asyncio.Lock()
        policy=ContextPolicy(**self.spec['context_policy'])
        models=[]
        def model(name,role):
            value=RecordedModel(context,gateway,name=name,role=role,policy=policy,failures=failures)
            models.append(value)
            return value
        participants=[]
        if domain == 'engineering':
            from .world_agents import engineering_agents
            participants = engineering_agents(model,gateway,lock,failures,self.spec['executor'])
        for member in (self.spec['teams'][domain] if domain == 'operational' else []):
            tools=[WorldTool(t,gateway,member['name'],lock,failures) for t in context.task.tools]
            prompt=(ROOT/'src/mac_bench/baselines/magentic_one/prompts'/member['prompt']).read_text()
            participants.append(AssistantAgent(member['name'],description=member['description'],
                model_client=model(member['name'],'executor'),tools=tools,
                system_message=prompt+'\n\n'+context.task.interaction_instructions,
                max_tool_iterations=self.spec['execution']['worker_tool_iterations_per_turn'],
                reflect_on_tool_use=True))
        team=MagenticOneGroupChat(participants,model_client=model('Orchestrator','orchestrator'),
            max_turns=self.spec['limits']['max_turns'],max_stalls=self.spec['limits']['max_stalls'],
            emit_team_events=True)
        trace=[]
        latest_instruction=''
        from autogen_agentchat.teams._group_chat._magentic_one import _magentic_one_orchestrator as native
        original_extract = native.extract_json_from_str
        def extract_ledger(text):
            # Upstream retries JSONDecodeError/TypeError, but its own extractor
            # raises plain ValueError for a non-JSON fenced block. Translate
            # parse/schema failures into the exception its native loop handles.
            try:
                values = original_extract(text)
                if len(values) != 1:
                    raise ValueError('Expected one progress ledger object')
                native.LedgerEntry.model_validate(values[0])
                return values
            except ValueError as exc:
                raise json.JSONDecodeError(str(exc),text,0) from exc
        native.extract_json_from_str = extract_ledger
        class LedgerLog(logging.Handler):
            def emit(self,record):
                if record.funcName != '_log_message':
                    return
                text=record.getMessage()
                payload={'kind':'orchestrator_log','agent_id':'Orchestrator','message':text}
                if text.startswith('Progress Ledger: '):
                    payload={'kind':'progress_ledger','agent_id':'Orchestrator',
                             'ledger':ast.literal_eval(text[len('Progress Ledger: '):])}
                elif text.startswith('Stall count exceeded'):
                    payload['kind']='replan'
                context.event(payload)
                with (directory/'ledgers.jsonl').open('a') as stream:
                    stream.write(json.dumps(payload)+'\n')
        logger=native.trace_logger
        previous_level=logger.level
        handler=LedgerLog()
        logger.addHandler(handler);logger.setLevel(logging.DEBUG)
        try:
            async for message in team.run_stream(task=context.task.instruction+'\n\n'+context.task.interaction_instructions):
                payload=message.model_dump(mode='json') if hasattr(message,'model_dump') else vars(message)
                with (directory/'messages.jsonl').open('a') as stream:
                    stream.write(json.dumps(payload,default=str)+'\n')
                if hasattr(message,'source'):
                    context.event({'kind':'message_send','agent_id':message.source,
                                   'message_type':type(message).__name__,'content':payload.get('content')})
                    if type(message).__name__=='TextMessage' and message.source=='MagenticOneOrchestrator':
                        latest_instruction=message.content
                    if type(message).__name__=='SelectSpeakerEvent':
                        for worker in message.content:
                            context.event({'kind':'delegate','parent':'Orchestrator','child':worker,
                                           'instruction':latest_instruction})
                trace.append(payload)
            if failures:
                raise failures[0]
            state=await team.save_state()
            (directory/'team_state.json').write_text(json.dumps(state,indent=2,default=str)+'\n')
        except BaseException:
            if failures:
                raise failures[0] from None
            raise
        finally:
            native.extract_json_from_str = original_extract
            logger.removeHandler(handler);logger.setLevel(previous_level)
            for client in models:
                await client.close()
        return {'stop_reason':'finished','trace':trace,'native_trace':'native/messages.jsonl'}
