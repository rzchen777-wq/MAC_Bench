"""Native agents with file navigation and execution transported into the candidate.

FileSurfer's reply procedure follows the pinned upstream _generate_reply;
the five browser operations await the world transport instead of reading host files.
"""
import asyncio
import json
import shlex

from autogen_agentchat.agents import CodeExecutorAgent
from autogen_core import FunctionCall
from autogen_core.code_executor import CodeExecutor, CodeResult
from autogen_core.models import UserMessage
from autogen_ext.agents.file_surfer import FileSurfer
from autogen_ext.agents.file_surfer._tool_definitions import (
    TOOL_OPEN_PATH, TOOL_PAGE_DOWN, TOOL_PAGE_UP, TOOL_FIND_NEXT, TOOL_FIND_ON_PAGE_CTRL_F,
)
from mac_bench.baselines.common.gateway import agent_scope, MethodStop


class WorldCodeExecutor(CodeExecutor):
    def __init__(self, gateway, lock, failures, *, python='python', timeout=120):
        self.gateway, self.lock, self.failures = gateway, lock, failures
        self.python, self.timeout = python, timeout

    async def start(self):
        pass  # The task owner already opened the sandbox.

    async def stop(self):
        pass  # Only the task owner may close the shared world.

    async def restart(self):
        pass  # Conversation reset must not reset the candidate.

    async def execute_code_blocks(self, code_blocks, cancellation_token):
        output = []
        with agent_scope(self.gateway.context, 'ComputerTerminal', 'executor'):
            try:
                for block in code_blocks:
                    if cancellation_token.is_cancelled():
                        raise asyncio.CancelledError()
                    language = block.language.lower()
                    if language in ('python', 'py'):
                        command = shlex.join([self.python, '-c', block.code])
                    elif language in ('sh', 'shell', 'bash'):
                        command = shlex.join(['bash' if language == 'bash' else 'sh', '-c', block.code])
                    else:
                        return CodeResult(1, '\n'.join(output + ['Unsupported language: '+language]))
                    async with self.lock:
                        result = await self.gateway.tool('terminal', {'command':command, 'timeout':self.timeout})
                    if 'exit_code' not in result:
                        return CodeResult(1, '\n'.join(output + [json.dumps(result)]))
                    output.append(result.get('output', ''))
                    # Native CodeResult is text-only. Carry the public recovery
                    # metadata through that projection so Coder can inspect a
                    # saved large output without rerunning the command.
                    metadata = {key: result[key] for key in ('full_output_path',
                        'output_truncated', 'output_bytes', 'instruction', 'capture_error') if key in result}
                    if metadata:
                        output.append('\nTerminal output metadata: '+json.dumps(metadata))
                    if result['exit_code']:
                        return CodeResult(result['exit_code'], '\n'.join(output))
            except MethodStop as stop:
                self.failures.append(stop.error)
                raise RuntimeError('World executor stopped: '+type(stop.error).__name__) from None
            except BaseException as error:
                self.failures.append(error)
                raise
        return CodeResult(0, '\n'.join(output))


# Runs only via the public terminal, inside /work/project. No host file reads.
_BROWSE = '''import json, pathlib, math
a=json.loads({arguments})
root=pathlib.Path('/work/project').resolve()
p=pathlib.Path(a['path'])
p=(root/p if not p.is_absolute() else p).resolve()
if not (p.is_relative_to(root) or p == root.parent/'TASK.md'):
    raise ValueError('Path is outside candidate project and public /work/TASK.md')
if p.is_dir():
    text='\\n'.join(x.name+('/' if x.is_dir() else '') for x in sorted(p.iterdir()))
else:
    if p.stat().st_size > 8*1024*1024: raise ValueError('File exceeds 8 MiB text preview limit; use Coder and ComputerTerminal for targeted inspection')
    text=p.read_text(encoding='utf-8')
size=5120
pages=max(1,math.ceil(len(text)/size))
page=max(0,min(a['page'],pages-1))
query=a.get('query')
found=None
if query:
    start=(page+1)*size if a.get('next') else page*size
    found=text.casefold().find(query.casefold(),start)
    if found<0: found=text.casefold().find(query.casefold(),0,start)
    if found>=0: page=found//size
print(json.dumps(dict(path=str(p),title=p.name,page=page,pages=pages,
    viewport=text[page*size:(page+1)*size],found=found),ensure_ascii=False))
'''


class RemoteBrowser:
    def __init__(self, gateway, lock, failures, python):
        self.gateway, self.lock, self.failures, self.python = gateway, lock, failures, python
        self.path, self.page_title = '/work/project', 'Candidate project'
        self.viewport_current_page, self.viewport_pages = 0, [None]
        self.viewport, self.query = 'Use open_path to inspect the candidate.', None

    async def navigate(self, name, arguments):
        path = arguments['path'] if name == 'open_path' else self.path
        page = 0 if name == 'open_path' else self.viewport_current_page
        if name == 'page_up': page -= 1
        if name == 'page_down': page += 1
        if name == 'find_on_page_ctrl_f': self.query = arguments['search_string']
        query = self.query if name in ('find_next', 'find_on_page_ctrl_f') else None
        values = json.dumps(dict(path=path, page=page, query=query, next=name == 'find_next'))
        code = _BROWSE.format(arguments=repr(values))
        try:
            async with self.lock:
                result = await self.gateway.tool('terminal', {'command':shlex.join([self.python, '-c', code])})
        except MethodStop as stop:
            self.failures.append(stop.error)
            raise RuntimeError('File transport stopped: '+type(stop.error).__name__) from None
        except BaseException as error:
            self.failures.append(error)
            raise
        if result.get('exit_code') != 0:
            self.viewport = 'File navigation failed: '+json.dumps(result)
            return
        state = json.loads(result['output'])
        self.path, self.page_title = state['path'], state['title']
        self.viewport_current_page, self.viewport_pages = state['page'], [None]*state['pages']
        self.viewport = state['viewport']
        if query and state['found'] == -1: self.viewport += '\nSearch text not found.'


class CandidateFileSurfer(FileSurfer):
    def __init__(self, name, model_client, browser):
        # Native FileSurfer.__init__ opens a host directory. Initialize its base
        # and conversation directly, substituting only the remote browser.
        from autogen_agentchat.agents import BaseChatAgent
        BaseChatAgent.__init__(self, name, FileSurfer.DEFAULT_DESCRIPTION)
        self._model_client, self._browser = model_client, browser
        self._chat_history = []

    async def _generate_reply(self, cancellation_token):
        # Same native history, tool set, response dispatch and viewport response.
        history, last_message = self._chat_history[:-1], self._chat_history[-1]
        assert isinstance(last_message, UserMessage)
        result = await self._model_client.create(
            messages=self._get_compatible_context(history + [
                UserMessage(source='user', content=f"Your file viewer is currently open to the file or directory '{self._browser.page_title}' with path '{self._browser.path}'."),
                UserMessage(source='user', content=last_message.content)]),
            tools=[TOOL_OPEN_PATH, TOOL_PAGE_DOWN, TOOL_PAGE_UP, TOOL_FIND_NEXT, TOOL_FIND_ON_PAGE_CTRL_F],
            cancellation_token=cancellation_token)
        if isinstance(result.content, str):
            return False, result.content
        if isinstance(result.content, list) and all(isinstance(c, FunctionCall) for c in result.content):
            for call in result.content:
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError as error:
                    return False, f'File surfer encountered an error decoding JSON arguments: {error}'
                with agent_scope(self._browser.gateway.context, self.name, 'executor'):
                    if call.name in ('open_path','page_up','page_down','find_on_page_ctrl_f','find_next'):
                        await self._browser.navigate(call.name, arguments)
            header, content = self._get_browser_state()
            return False, header.strip()+'\n=======================\n'+content
        return False, 'TERMINATE'


def engineering_agents(model, gateway, lock, failures, config):
    from autogen_ext.agents.magentic_one import MagenticOneCoderAgent
    executor = WorldCodeExecutor(gateway, lock, failures, **config)
    return [
        CandidateFileSurfer('FileSurfer', model('FileSurfer', 'executor'),
            RemoteBrowser(gateway, lock, failures, config['python'])),
        MagenticOneCoderAgent('Coder', model_client=model('Coder', 'executor')),
        CodeExecutorAgent('ComputerTerminal', code_executor=executor,
            model_client=None, description=(
                'Executes complete fenced sh/bash/python code blocks from Coder or '
                'Orchestrator inside the shared candidate. Returns real execution output. '
                'Cannot reason, implement from prose instructions, or execute unfenced commands.')),
    ]
