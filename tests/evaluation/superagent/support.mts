import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { startHarness } from '../../../src/mac_bench/baselines/superagent/sessions.ts'

const require = createRequire(new URL('../../../src/mac_bench/baselines/superagent/package.json', import.meta.url))
export const { scopeOf } = await import(require.resolve('@deepseek-ai/dsh-scope'))
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'))

export class ScriptedModel extends LlmAdapter {
  readonly requests = []
  readonly respond: (request: any) => any
  constructor(respond: (request: any) => any) { super(); this.respond = respond }
  async resolveModel(provider, id) { return { provider, id, name: id, context: { contextWindow: 131072 } } }
  async *stream(request) {
    this.requests.push(request)
    assert.ok(this.requests.length <= 30, 'Native test entered an unexpected loop')
    const response = this.respond(request)
    const block = typeof response === 'string' ? { type: 'text', text: response }
      : { type: 'tool-call', id: `call-${this.requests.length}`, name: response.name,
        arguments: JSON.stringify(response.arguments) }
    yield { type: 'block-start', index: 0, blockType: block.type }
    yield typeof response === 'string' ? { type: 'text-delta', index: 0, text: response }
      : { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments }
    yield { type: 'block-end', index: 0, block }
    yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 20, totalTokens: 120 } }
    yield { type: 'finish', reason: { kind: typeof response === 'string' ? 'stop' : 'tool-calls' } }
  }
}

export async function quiescent(context, agent) {
  do { await agent.whenIdle() } while (await context.subagents.waitForChildren(agent))
  await agent.whenIdle()
}

export async function fixture(options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'mac-superagent-native-'))
  const workspace = options.workspace ?? join(directory, 'work')
  if (!options.workspace) await mkdir(workspace, { recursive: true })
  const context = await startHarness({
    stateDirectory: join(directory, 'state'), workspace,
    model: { provider: 'fixture', model: 'fixture-model' }, modelPlugins: [], ...options,
    world: {
      filesystem: { id: 'filesystem', name: '@deepseek-ai/dsh-fs-local', config: { cwd: workspace } },
      subprocess: { id: 'subprocess', name: '@deepseek-ai/dsh-subprocess-local' },
      services: [{ id: 'sandbox', name: '@deepseek-ai/dsh-sandbox-local' },
        { id: 'shell-env', name: '@deepseek-ai/dsh-shell-env' }],
      shell: wait => ({ id: 'shell', name: '@deepseek-ai/dsh-bash-sandbox',
        config: { timeoutMs: wait, maxTimeoutMs: wait } }),
      ...options.world,
    },
  })
  return { context, workspace, directory, async dispose() {
    await context.fiber.dispose()
    await rm(directory, { recursive: true, force: true })
  } }
}
