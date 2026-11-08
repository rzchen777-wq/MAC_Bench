/** One native Superagent episode; the Python evaluator owns world and budget. */
import { readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'
import { startHarness } from './sessions.ts'
import { Runtime } from './runtime.ts'
import type { PublicTask } from './source.ts'
import { worldProviders } from './world.ts'

interface Options {
  task: PublicTask
  stateDirectory: string
  worldURL: string
  tools: ToolSchema[]
  model: string
  modelURL: string
  thinking: 'enabled' | 'disabled'
  maxTokens: number
  contextWindow: number
  backgroundMs: number
}

const options: Options = JSON.parse(await readFile(process.argv[2], 'utf8'))
let runtime: Runtime | undefined
let stopped = false
let closing: Promise<void> | undefined
const stop = () => {
  stopped = true
  if (runtime) {
    closing ??= runtime.close()
    closing.catch(() => {}) // Joined below, including a failed shutdown.
  }
}
process.on('SIGTERM', stop)
process.on('SIGINT', stop)
const context = await startHarness({
  stateDirectory: options.stateDirectory, workspace: options.task.workspace,
  model: { provider: 'deepseek-official', model: options.model },
  modelPlugins: [{ id: 'deepseek', name: '@deepseek-ai/dsh-llm-deepseek-api-key', config: {
    baseURL: options.modelURL, apiKeyEnv: 'MAC_SUPERAGENT_MODEL_KEY',
    thinking: options.thinking, maxTokens: options.maxTokens,
    models: [{ id: options.model, contextWindow: options.contextWindow }],
  } }],
  world: worldProviders({ url: options.worldURL, tokenEnv: 'MAC_SUPERAGENT_WORLD_KEY',
    workspace: options.task.workspace, timingFile: join(options.stateDirectory, 'timings.jsonl') }, options.tools),
})
const emit = (value: object) => process.stdout.write('MAC_BENCH_EVENT ' + JSON.stringify(value) + '\n')
const worldTools = new Set(['read', 'read_image', 'write', 'edit', 'glob', 'grep', 'bash',
  ...options.tools.map(tool => tool.name)])
context.on('agent/status', ({ agent, status }) => emit({
  kind: status === 'running' ? 'agent_start' : 'agent_end', agent_id: agent.id,
}))
context.on('tools/execute', (execution, next) => {
  if (!worldTools.has(execution.name)) {
    emit({ kind: 'method_control_call', agent_id: execution.agent?.id,
      tool: execution.name, tool_call_id: execution.callId, arguments: execution.arguments })
  }
  return next()
})
try {
  runtime = new Runtime(context, options.task, options.stateDirectory, options.backgroundMs)
  const result = stopped ? { reason: 'stopped', tickets: [], replies: [] } : await runtime.run()
  await runtime.close()
  await writeFile(join(options.stateDirectory, 'result.json'), JSON.stringify(result, null, 2) + '\n')
} finally {
  try { await (closing ?? runtime?.close()) }
  finally { await context.fiber.dispose() }
  process.off('SIGTERM', stop)
  process.off('SIGINT', stop)
}
