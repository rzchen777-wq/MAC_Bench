"""Run the source-owned Superagent orchestration against one public task world."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread
import time

from mac_bench.evaluation.episodes.budget import BudgetExceeded
from .bridge import WorldBridge
from .model_relay import ModelRelay

SOURCE = Path(__file__).resolve().parent


def node_runtime():
    node = shutil.which('node')
    if node is None:
        raise RuntimeError('Superagent requires Node.js 24 or newer on PATH')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    if int(version.removeprefix('v').split('.')[0]) < 24:
        raise RuntimeError('Superagent requires Node.js 24 or newer; found ' + version)
    return node


def runtime_dependencies():
    """Resolve the installed harness and search binary before allocating a world."""
    node = node_runtime()
    script = """
        import { readFile } from 'node:fs/promises';
        import { resolveRgPath } from '@deepseek-ai/dsh-tool-fs-search';
        const manifest = JSON.parse(await readFile('./package.json', 'utf8'));
        for (const name of Object.keys(manifest.dependencies)) import.meta.resolve(name);
        await Promise.all([import('./runtime.ts'), import('./sessions.ts'), import('./world.ts')]);
        console.log(await resolveRgPath());
    """
    try:
        rg = subprocess.check_output([node, '--input-type=module', '-e', script],
            cwd=SOURCE, text=True, stderr=subprocess.PIPE, timeout=30).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Install Superagent dependencies with pnpm install --frozen-lockfile '
                           f'in {SOURCE}:\n{exc.stderr}') from exc
    executable = Path(rg)
    if not executable.is_file():
        raise FileNotFoundError(f'Superagent search executable is missing: {executable}')
    return node, executable


def run(context, spec):
    node, rg = runtime_dependencies()
    directory = Path(context.artifacts).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / 'harness'
    if context.open_execution is None:
        raise RuntimeError('This task has no public execution workspace')
    execution = context.open_execution()
    # The search executable is an installed dependency, uploaded at runtime;
    # neither upstream source nor the binary is vendored into the repository.
    execution.install_executable('rg', rg.read_bytes())
    tools = [tool.as_function()['function'] for tool in context.task.tools if tool.name != 'terminal']
    instructions = context.task.interaction_instructions
    if tools:
        instructions += ('\nUse the supplied application tools to read and change business state. '
            'The shared workspace contains the public task materials and is available for scripts and notes. '
            'These files describe the initial task; live application state is accessed through the application tools.')
        if execution.public_tool_guide:
            instructions += ('\nWorkers can call the same public application tools from Python scripts using mac_tools, '
                'or the mac-tools command. Script calls use the same application access and tool budget; '
                'evaluation logs attribute each call to the worker that launched the script. '
                f'Read {execution.public_tool_guide} for usage, complete saved results and shared files. '
                'The router delegates application operations to workers.')
    else:
        # The native bash tool reaches exactly the existing terminal workspace.
        instructions = instructions.replace('through terminal', 'through bash')
    with ModelRelay(context.client, **spec.get('model_transport', {})) as model, WorldBridge(context, execution) as world:
        options = {'task': {'taskId': context.task.task_id, 'workspace': execution.workspace,
                    'instruction': context.task.instruction.replace('through terminal', 'through bash') if not tools
                    else context.task.instruction, 'interactionInstructions': instructions},
            'stateDirectory': str(state), 'worldURL': world.url, 'tools': tools,
            'model': context.client.config.model, 'modelURL': model.base_url,
            'thinking': context.client.config.thinking_mode or 'disabled',
            'maxTokens': spec['model_parameters']['max_tokens'],
            'contextWindow': spec['model_parameters'].get('context_window', 1_000_000),
            'backgroundMs': int(spec['execution']['background_grace_seconds'] * 1000)}
        config = directory / 'episode.json'
        config.write_text(json.dumps(options, indent=2) + '\n')
        env = {key: value for key, value in os.environ.items()
               if not re.search(r'TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL', key, re.I)}
        env.update(MAC_SUPERAGENT_MODEL_KEY=model.token, MAC_SUPERAGENT_WORLD_KEY=world.token)
        stop_error = None
        reader_errors = []
        with (directory / 'runner.stderr.log').open('w') as stderr, (directory / 'runner.stdout.log').open('w') as stdout:
            process = subprocess.Popen([node, str(SOURCE / 'main.ts'), str(config)], cwd=SOURCE,
                env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)

            def read_events():
                try:
                    for line in process.stdout:
                        stdout.write(line)
                        stdout.flush()
                        if line.startswith('MAC_BENCH_EVENT '):
                            context.event(json.loads(line.removeprefix('MAC_BENCH_EVENT ')))
                except Exception as exc:
                    reader_errors.append(exc)

            reader = Thread(target=read_events, name='superagent-events')
            reader.start()
            try:
                while process.poll() is None:
                    stop_error = model.stop_error or world.stop_error or execution.stop_error
                    elapsed = time.monotonic() - context.budget.started
                    if (context.budget.limits.max_wall_seconds is not None
                            and elapsed >= context.budget.limits.max_wall_seconds):
                        stop_error = BudgetExceeded('wall_seconds')
                    usage = context.budget.snapshot()
                    if usage['input_tokens'] + usage['output_tokens'] >= context.budget.limits.max_tokens:
                        stop_error = BudgetExceeded('tokens')
                    if stop_error or reader_errors:
                        process.terminate()
                        break
                    time.sleep(.1)
                process.wait(timeout=context.client.config.timeout_seconds + 30)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                reader.join()
                process.stdout.close()
        if reader_errors:
            raise reader_errors[0]
        if stop_error or model.stop_error or world.stop_error or execution.stop_error:
            raise stop_error or model.stop_error or world.stop_error or execution.stop_error
        if process.returncode:
            raise RuntimeError(f'Superagent runner exited with code {process.returncode}; see runner.stderr.log')
        result = json.loads((state / 'result.json').read_text())
    return {'stop_reason': result['reason'], 'tickets': result['tickets'], 'replies': result['replies']}
