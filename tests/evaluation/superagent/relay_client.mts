import assert from 'node:assert/strict'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { fixture, quiescent } from './support.mts'

const environment = await fixture({
  model: { provider: 'deepseek-official', model: 'deepseek-v4-flash' },
  modelPlugins: [{ id: 'deepseek', name: '@deepseek-ai/dsh-llm-deepseek-api-key', config: {
    baseURL: process.env.MAC_TEST_RELAY_URL, apiKeyEnv: 'MAC_TEST_RELAY_KEY',
    thinking: 'enabled', maxTokens: 4096,
  } }],
})
try {
  const sessions = new Sessions(environment.context, environment.workspace)
  const agent = await sessions.create('wire-worker', 'worker', 'Read the public task.')
  const errors = []
  environment.context.on('agent/error', ({ error }) => errors.push(error))
  await sessions.send(agent, 'first', 'Inspect the task.\n' + 'The shared workspace is ready.\n'.repeat(400))
  await quiescent(environment.context, agent)
  await sessions.send(agent, 'second', 'Continue the same task.')
  await quiescent(environment.context, agent)
  const compacted = await environment.context.commands.execute(agent, '/compact', [], new AbortController().signal)
  assert.equal(compacted?.result.kind, 'success', JSON.stringify(compacted))
  assert.ok(compacted?.result.text?.startsWith('Compacted '), JSON.stringify(compacted))
  await sessions.send(agent, 'after-compaction', 'Continue using the summarized task context.')
  await quiescent(environment.context, agent)
  assert.deepEqual(errors, [])
  assert.ok(JSON.stringify(agent.session.snapshotEvents()).includes('The shared workspace is ready.'))
} finally {
  await environment.dispose()
}
