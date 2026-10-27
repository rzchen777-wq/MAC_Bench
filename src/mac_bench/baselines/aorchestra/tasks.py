"""Official orchestration/sub-agent loops over one shared benchmark candidate."""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from mac_bench.evaluation.episodes.identity import call_scope, call_identity
from mac_bench.baselines.common.config import ROOT, load_spec, resolve, tree_hash
from mac_bench.baselines.common.source import source_modules
from mac_bench.baselines.common.gateway import Gateway, MethodStop, agent_scope
from mac_bench.baselines.aorchestra.memory import worker_memory


def _coordinator_runner(runner_type):
    runner = runner_type()
    runner.step_timeout = None
    return runner


def _propagate_worker_stop(stop):
    from mac_bench.runtime.llm.client import ModelOutputTruncatedError
    # A billed malformed worker response belongs to native failed-delegation
    # handling, unlike budget/provider/storage errors that terminate the method.
    if isinstance(stop.error, ModelOutputTruncatedError) and stop.error.response is not None:
        raise stop.error
    raise stop
from mac_bench.baselines.aorchestra.prompts import WorldPrompt, install_worker_prompts
from mac_bench.baselines.aorchestra.protocol import action_messages, complete_action


class AOrchestra:
    name = 'aorchestra'

    def __init__(self, config=None):
        self.spec = load_spec(self.name, config)

    def run(self, context):
        directory = resolve(self.spec, context, assets={'prompts': tree_hash(ROOT/'src/mac_bench/baselines/aorchestra/prompts')})
        root = ROOT / self.spec['source']['directory']
        with source_modules(root, ('base','benchmark','aorchestra'), workdir=directory):
            try:
                return asyncio.run(self._run(context, directory))
            except MethodStop as stop:
                raise stop.error
            finally:
                import sys
                logs = sys.modules.get('base.engine.logs')
                if logs and logs.logger.file_output:
                    logs.logger.file_output.close()

    async def _run(self, context, directory):
        from base.engine.async_llm import AsyncLLM, LLMsConfig
        from base.agent.memory import Memory
        from base.engine.logs import logger
        from benchmark.common.env import BasicInfo
        from benchmark.common.runner import Runner
        from aorchestra.main_agent import MainAgent
        from aorchestra.tools.delegate import DelegateTaskTool
        import aorchestra.tools.delegate as delegate_module
        import aorchestra.subagents.react_agent as react_module
        memory_template = (ROOT/'src/mac_bench/baselines/aorchestra/prompts/memory.txt').read_text()
        class WorkerAgent(react_module.ReActAgent):
            def reset(self, env_info):
                super().reset(env_info)
                self.max_steps = env_info.max_steps

            def _build_prompt(self, **kwargs):
                state = context.budget.snapshot()
                remaining = max(0, context.budget.limits.max_model_calls -
                    state['model_call_attempts'])
                tokens = max(0, context.budget.limits.max_tokens -
                    state['input_tokens'] - state['output_tokens'])
                notice = ('\n\nSHARED EPISODE BUDGET\n'
                    f'{remaining} model calls remain BEFORE this response; '
                    f'{tokens} reported tokens remain. This budget includes all workers, '
                    'memory compression, report summarization, and coordinator decisions.\n')
                if remaining <= 5:
                    notice += ('Return finish now with available findings and unresolved work '
                        'so the coordinator can receive your report within the shared budget.\n')
                return super()._build_prompt(**kwargs) + notice

            async def step(self, observation, history, **kwargs):
                # The upstream Runner omits these arguments, leaving STEP at 1/30.
                return await super().step(observation, history,
                    current_step=len(history)+1, max_steps=self.max_steps)

        delegate_module.ReActAgent = WorkerAgent
        gateway = Gateway(context, self.spec['model_parameters'])
        control_contracts = {}
        model = context.client.config.model
        domain = 'engineering' if {t.name for t in context.task.tools} == {'terminal'} else 'operational'
        branch = 'terminalbench' if domain == 'engineering' else 'gaia'
        install_worker_prompts(react_module, context.task.interaction_instructions)
        # The default logger writes outside the episode; redirect to native assets.
        logger.console_output = False

        class RecordedLLM(AsyncLLM):
            def __init__(self):
                self.config = SimpleNamespace(model=model)
                self.sys_msg = None
                self.input_tokens = self.output_tokens = self.calls = 0
            async def __call__(self, prompt, max_tokens=None):
                phase = 'memory' if 'memory compression module' in str(prompt) else call_identity().get('phase','execution')
                with call_scope(phase=phase):
                    options = {'max_tokens': max_tokens} if max_tokens is not None else {}
                    async def complete(messages):
                        response = await gateway.async_complete(messages, **options)
                        self.calls += 1
                        self.input_tokens += response.usage.get('prompt_tokens',0)
                        self.output_tokens += response.usage.get('completion_tokens',0)
                        return response
                    if phase == 'execution':
                        coordinator = call_identity().get('agent_id') == 'main'
                        contracts = control_contracts if coordinator else {
                            name: tool.input_schema for name, tool in definitions.items()}
                        if not coordinator:
                            contracts = {**contracts, 'finish': {'type': 'object'}}
                        messages = action_messages(prompt, contracts, coordinator=coordinator)
                        response = await complete_action(complete, messages, contracts,
                                                         event=context.event)
                    else:
                        response = await complete([{'role':'user','content':prompt}])
                return response.content
            def get_usage_summary(self):
                return {'model':model,'total_cost':None,'total_input_tokens':self.input_tokens,
                        'total_output_tokens':self.output_tokens,'call_count':self.calls}

        # Scoped upstream modules are discarded on exit: no factory/global patch leaks.
        LLMsConfig.default = staticmethod(lambda: SimpleNamespace(get=lambda name: SimpleNamespace(model=model)))
        delegate_module.create_llm_instance = lambda config: RecordedLLM()
        counter = 0
        definitions = {('execute' if domain == 'engineering' else t.name):t for t in context.task.tools}
        action_space = '\n\n'.join('### '+name+'\n'+t.description+'\n'+json.dumps(t.input_schema)
            for name,t in definitions.items()) + (
                '\n\n### finish\nReport to the main agent with params: '
                '{"status":"done|partial","result":"observed findings or outcome",'
                '"completed":["work actually completed"],"issues":["remaining issues"],'
                '"message":"handover notes"}. status describes THIS delegated assignment. '
                'A report does not itself change the application or source workspace.')

        class Environment:
            def __init__(self, *, main=False):
                self.instruction = context.task.instruction
                self.allowed = set(definitions)
                self.main = main
            def get_basic_info(self):
                return BasicInfo(env_id=context.task.task_id,instruction=self.instruction,
                    action_space=action_space,max_steps= self_spec['upstream_limits']['main_attempts']
                    if self.main else self_spec['upstream_limits']['sub_agent_steps'])
            async def reset(self, seed=None):
                return {'instruction':self.instruction,'message':'Continue in the shared live world. No data was reset.'}
            async def step(self, action):
                name = action.get('action')
                if self.main:
                    return action.get('result',{}), 0.0, name=='submit', {'submitted':name=='submit'}
                if name=='finish':
                    return action.get('params',{}),0.0,True,{'finished':True,'finish_result':action.get('params',{})}
                if domain == 'engineering' and name == 'terminal':
                    name = 'execute'
                if name not in self.allowed:
                    result = {'error':{'code':'unknown_or_unassigned_tool'}}
                else:
                    result = await gateway.tool(definitions[name].name, action.get('params',{}))
                return result,0.0,False,{'last_action_result':result}

        self_spec = self.spec
        class WorkerRunner(Runner):
            async def run(self, agent, env):
                nonlocal counter
                counter += 1
                worker_id = 'worker-'+str(counter)
                agent.memory = worker_memory(Memory, llm=agent.llm,
                    instruction=agent.task_instruction, context=agent.context,
                    template=memory_template)
                with agent_scope(context,worker_id,'executor'):
                    context.event({'kind':'delegate','parent':'main','child':worker_id,
                        'instruction':agent.task_instruction,'tools':sorted(env.allowed)})
                    try:
                        return await super().run(agent,env)
                    except MethodStop as stop:
                        context.event({'kind':'worker_model_stop','agent_id':worker_id,
                                       'error_type':type(stop.error).__name__})
                        _propagate_worker_stop(stop)

        class Delegate(DelegateTaskTool):
            async def __call__(self, task_instruction, model, context='', tools=None):
                if domain == 'engineering' and tools is not None:
                    tools = ['execute' if name == 'terminal' else name for name in tools]
                allowed = set(definitions) if tools is None else set(tools)
                if not allowed <= set(definitions):
                    return {'error':'Delegated tools are not in the task contract'}
                local = Environment()
                local.allowed = allowed
                self.env = local
                context_event = {'kind':'method_control_call','tool':'delegate_task','agent_id':'main'}
                gateway.context.event(context_event)
                result = await super().__call__(task_instruction,model,context,tools)
                if 'statistics' in result:
                    result['statistics']['max_steps'] = self_spec['upstream_limits']['sub_agent_steps']
                return result
            async def _summarize_trace(self, trace, task_instruction):
                with call_scope(agent_id='main',role='orchestrator',phase='trajectory_summary'):
                    from mac_bench.baselines.aorchestra.trajectory import summarize_trace
                    return await summarize_trace(gateway,trace,task_instruction)

        class Submit:
            name = 'submit'
            async def __call__(self, **params):
                context.event({'kind':'method_control_call','tool':'submit','agent_id':'main'})
                return {'submitted':True,'message':params.get('summary','Method finished; grading is external.')}

        worker_runner = WorkerRunner()
        delegate = Delegate(Environment(),worker_runner,[model],benchmark_type=branch)
        control_contracts.update(delegate_task=delegate.parameters,
                                 submit={'type':'object'})
        main = MainAgent(llm=RecordedLLM(),sub_models=[model],tools=[delegate,Submit()],
            subagent_tools=list(context.task.tools),benchmark_type=branch,
            prompt_builder=WorldPrompt(domain, context.task.interaction_instructions),
            max_attempts=self.spec['upstream_limits']['main_attempts'])
        # One coordinator step awaits an entire delegated worker episode.
        # The upstream 600s single-step timer would cancel a healthy assignment
        # and synthesize done=True. Shared model/action/wall budgets already
        # govern the delegation, and individual worker steps retain their timer.
        coordinator_runner = _coordinator_runner(Runner)
        with agent_scope(context,'main','orchestrator'):
            result = await coordinator_runner.run(main,Environment(main=True))
        payload = asdict(result)
        (directory/'aorchestra_trace.json').write_text(json.dumps(payload,indent=2,default=str))
        return {'stop_reason':'finished' if result.done else 'main_attempt_limit',
                'trace':payload['trace'],'native_trace':'native/aorchestra_trace.json'}
