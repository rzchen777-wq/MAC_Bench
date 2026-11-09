import assert from 'node:assert/strict'
import { fixture, ScriptedModel, quiescent, scopeOf } from './support.mts'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const environment = await fixture({ workspace: process.env.MAC_TEST_WORKSPACE,
  world: worldProviders({ url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY',
    workspace: process.env.MAC_TEST_WORKSPACE! }, JSON.parse(process.env.MAC_TEST_TOOLS!)),
})
const { context, workspace } = environment
let delegated = false
let updated = false
let checked = false
let sawUpdate = false
const adapter = new ScriptedModel(request => {
  if (request.sessionId === 'application-worker') {
    if (!delegated) {
      delegated = true
      return { name: 'subagent', arguments: { description: 'Update the shared application',
        prompt: 'Call update_record to set the shared record to confirmed.' } }
    }
    if (updated && !checked) {
      checked = true
      return { name: 'read_record', arguments: {} }
    }
    sawUpdate ||= request.messages.some(message => message.role === 'tool'
      && JSON.stringify(message).includes('confirmed'))
    return 'Waiting for or checking the application update.'
  }
  if (!updated) {
    updated = true
    return { name: 'update_record', arguments: { status: 'confirmed' } }
  }
  return 'Updated the application record.'
})
context.llm.registerAdapter(['fixture'], adapter)
try {
  const sessions = new Sessions(context, workspace)
  const worker = await sessions.create('application-worker', 'worker', 'Update and verify the application record.')
  const router = await sessions.create('application-router', 'router', 'Route requests.')
  const workerTools = worker.ctx.tools.schemas(scopeOf(worker.ctx))
  assert.ok(workerTools.some(tool => tool.name === 'update_record'))
  assert.ok(!router.ctx.tools.schemas(scopeOf(router.ctx)).some(tool => tool.name === 'update_record'))
  await sessions.send(worker, 'task', 'Delegate the update, then check the shared record.')
  await quiescent(context, worker)
  assert.ok(sawUpdate, JSON.stringify(worker.session.snapshotEvents()))
  assert.equal((await context.subagents.listChildren(worker.id)).length, 1)
} finally {
  await environment.dispose()
}
