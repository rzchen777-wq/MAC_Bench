import assert from 'node:assert/strict'
import { join } from 'node:path'
import { fixture, ScriptedModel } from './support.mts'
import { Runtime } from '../../../src/mac_bench/baselines/superagent/runtime.ts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const environment = await fixture({ workspace: process.env.MAC_TEST_WORKSPACE,
  world: worldProviders({ url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY',
    workspace: process.env.MAC_TEST_WORKSPACE! }),
})
const { context, workspace, directory } = environment
const runtime = new Runtime(context, { taskId: 'remote-process', workspace,
  instruction: 'Create result.txt containing done and verify it.', interactionInstructions: 'Use the shared workspace.' }, join(directory, 'state'))
let routed = false, started = false, reported = false, replied = false, noticed = false, collected = false
const adapter = new ScriptedModel(request => {
  if (request.sessionId === 'superagent-router') {
    if (!routed) {
      routed = true
      return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept', trigger_id: 'task:remote-process',
        title: 'Create and check the file' }] } }
    }
    const ticket = runtime.tickets.list()[0]
    if (ticket?.status === 'ended' && !replied) {
      replied = true
      return { name: 'orchestrator_reply', arguments: { ticket_id: ticket.id, text: 'Created and verified result.txt.' } }
    }
    return 'The worker is handling it.'
  }
  if (!started) {
    started = true
    return { name: 'bash', arguments: { description: 'Run the public check directly',
      command: 'sh ./develop.sh check' } }
  }
  noticed ||= request.messages.some(message => message.role === 'user' && message.source.kind === 'tool-jobs')
  if (noticed && !collected) {
    collected = true
    const job = context.jobs.list(request.sessionId).find(job => job.status === 'completed')
    assert.ok(job, 'Completion notification must refer to a finished managed job')
    return { name: 'job_output', arguments: { job_id: job.id } }
  }
  const output = request.messages.some(message => message.role === 'tool' && JSON.stringify(message).includes('REMOTE_FINISHED'))
  if (noticed && output && !reported) {
    reported = true
    return { name: 'orchestrator_report', arguments: { state: 'ready', summary: 'The command created result.txt and read it back.' } }
  }
  return reported ? 'Finished.' : 'Waiting for the command.'
})
context.llm.registerAdapter(['fixture'], adapter)
try {
  const result = await runtime.run()
  assert.equal(result.reason, 'ready', JSON.stringify(result))
  assert.ok(noticed && collected)
  assert.equal(await context.fs.readText(await context.fs.resolve('result.txt')), 'done\n')
  assert.equal(await context.fs.readText(await context.fs.resolve('starts')), 'started\n')
  assert.ok(context.jobs.list(result.tickets[0].workerSessionId).every(job => !['running', 'stopping'].includes(job.status)))
  const searcher = await runtime.sessions.create('search-fixture', 'worker', 'Inspect the shared result.')
  for (const [name, args] of [['grep', { pattern: 'done', path: workspace }], ['glob', { pattern: '*.txt', path: workspace }]]) {
    const result = await context.tools.execute({ callId: `search-${name}`, name, arguments: args,
      agent: searcher, signal: AbortSignal.timeout(15000) })
    assert.equal(result.isError, false, JSON.stringify(result))
    assert.ok(JSON.stringify(result.content).includes('result.txt'), JSON.stringify(result))
  }
} finally {
  await runtime.close()
  await environment.dispose()
}
