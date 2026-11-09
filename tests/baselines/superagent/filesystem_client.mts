import assert from 'node:assert/strict'
import { fixture, ScriptedModel, quiescent } from './support.mts'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const environment = await fixture({ workspace: process.env.MAC_TEST_WORKSPACE,
  world: worldProviders({ url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY',
    workspace: process.env.MAC_TEST_WORKSPACE! }),
})
const { context, workspace } = environment
const read = { name: 'read', arguments: { file_path: 'shared.txt' } }
const edit = (old_string, new_string) => ({ name: 'edit', arguments: {
  file_path: 'shared.txt', old_string, new_string,
} })
const actions = {
  'file-worker-a': [read, 'Read the initial value.', edit('initial', 'overwritten'), 'Need to read the current value.',
    read, edit('worker-b', 'verified'), 'Verified the current value.'],
  'file-worker-b': [read, edit('initial', 'worker-b'),
    { name: 'bash', arguments: { description: 'Check the edited file in the shared workspace', command: 'cat shared.txt' } },
    'Checked the edited value.'],
}
const adapter = new ScriptedModel(request => {
  const queue = actions[request.sessionId]
  assert.ok(queue?.length, 'Unexpected native model call')
  return queue.shift()
})
context.llm.registerAdapter(['fixture'], adapter)
try {
  const sessions = new Sessions(context, workspace)
  const a = await sessions.create('file-worker-a', 'worker', 'Read and update the shared file.')
  const b = await sessions.create('file-worker-b', 'worker', 'Read and update the same shared file.')
  await sessions.send(a, 'read', 'Read shared.txt.')
  await quiescent(context, a)
  await sessions.send(b, 'change', 'Update the shared file and verify it with the shell.')
  await quiescent(context, b)
  assert.ok(JSON.stringify(b.session.snapshotEvents()).includes('worker-b'))
  await sessions.send(a, 'stale', 'Try your previously planned edit.')
  await quiescent(context, a)
  assert.ok(JSON.stringify(a.session.snapshotEvents()).includes('FS_STALE_VERSION'))
  await sessions.send(a, 'refresh', 'Read the changed file and update that version.')
  await quiescent(context, a)
  assert.equal(await context.fs.readText(await context.fs.resolve('shared.txt')), 'verified\n')
} finally {
  await environment.dispose()
}
