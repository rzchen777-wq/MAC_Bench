import assert from 'node:assert/strict'
import { fixture, ScriptedModel, quiescent } from './support.mts'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const workspace = process.env.MAC_TEST_WORKSPACE!
const environment = await fixture({ workspace, world: worldProviders({
  url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY', workspace,
  timingFile: process.env.MAC_TEST_TIMING_FILE,
}) })
const { context } = environment
const actions = [
  { name: 'read', arguments: { file_path: 'nested/file.txt' } },
  'Read the nested file.',
  { name: 'bash', arguments: { description: 'Update workspace guidance and shared content',
    command: "printf 'ROOT_GUIDANCE_TWO\\n' > AGENTS.md; printf 'NESTED_GUIDANCE_TWO\\n' > nested/AGENTS.md; printf 'updated\\n' > nested/file.txt" } },
  'Updated shared guidance.',
  { name: 'read', arguments: { file_path: 'nested/file.txt' } },
  'Read the updated file.',
  { name: 'bash', arguments: { description: 'Remove obsolete workspace guidance',
    command: 'rm AGENTS.md nested/AGENTS.md' } },
  'Removed shared guidance.',
  { name: 'read', arguments: { file_path: 'nested/file.txt' } },
  'Read after removal.',
]
const adapter = new ScriptedModel(() => {
  assert.ok(actions.length)
  return actions.shift()
})
context.llm.registerAdapter(['fixture'], adapter)
try {
  const sessions = new Sessions(context, workspace)
  const worker = await sessions.create('instructions-reader', 'worker', 'Read the shared file and follow current guidance.')
  const editor = await sessions.create('instructions-editor', 'worker', 'Update shared workspace guidance when requested.')
  await sessions.send(worker, 'first', 'Read the nested file.')
  await quiescent(context, worker)
  assert.ok(JSON.stringify(adapter.requests).includes('NESTED_GUIDANCE_ONE'))
  await sessions.send(editor, 'edit', 'Update the guidance and shared file.')
  await quiescent(context, editor)
  await sessions.send(worker, 'read-updated', 'Read the shared file again.')
  await quiescent(context, worker)
  const changed = adapter.requests.at(-1)
  const updates = changed.messages.filter(message => message.source.kind === 'agent-instructions')
  assert.ok(JSON.stringify(updates).includes('ROOT_GUIDANCE_TWO'))
  assert.ok(JSON.stringify(updates).includes('NESTED_GUIDANCE_TWO'))
  assert.equal(await context.fs.readText(await context.fs.resolve('nested/file.txt')), 'updated\n')
  await sessions.send(editor, 'remove', 'Remove obsolete guidance.')
  await quiescent(context, editor)
  await sessions.send(worker, 'read-removed', 'Read the shared file again.')
  await quiescent(context, worker)
  const removals = adapter.requests.at(-1).messages.filter(message => message.source.kind === 'agent-instructions')
  assert.ok(removals.some(message => message.source.changes?.some(change => change.action === 'remove')),
    'Removed guidance must reach the next model request')
  assert.equal(actions.length, 0)
} finally {
  await environment.dispose()
}
