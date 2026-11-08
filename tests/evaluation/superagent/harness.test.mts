import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { fixture, ScriptedModel, quiescent, scopeOf } from './support.mts'

test('native roles support child messaging and continuation in one shared workspace', { timeout: 20_000 }, async () => {
  const environment = await fixture()
  const { context, workspace } = environment
  try {
    let childId
    let delegated = false
    let continued = false
    const childPresets = []
    const childTimeouts = []
    const childMessages = new Set()
    const adapter = new ScriptedModel(request => {
      const text = JSON.stringify(request.messages)
      if (request.sessionId === 'worker-test') {
        if (!delegated) {
          delegated = true
          return { name: 'subagent', arguments: { description: 'Inspect the shared workspace', prompt: 'Check the workspace and report a finding to your parent.' } }
        }
        if (text.includes('CONTINUE_CHILD') && !continued) {
          continued = true
          return { name: 'send_message', arguments: { agent_id: childId, message: 'Check the SECOND_FINDING in the same workspace.' } }
        }
        return 'Waiting for findings.'
      }
      childId = request.sessionId
      const child = context.agents.get(childId)
      childPresets.push(context.agentPresets.composedPreset(child.ctx))
      childTimeouts.push(context.agentPresets.serviceFor(child, 'shell').config.timeoutMs.get())
      const finding = text.includes('SECOND_FINDING') ? 'Second finding confirmed.' : 'First finding confirmed.'
      if (!childMessages.has(finding)) {
        childMessages.add(finding)
        return { name: 'send_message', arguments: { agent_id: 'worker-test', message: finding } }
      }
      return 'Reported to parent.'
    })
    context.llm.registerAdapter(['fixture'], adapter)
    const sessions = new Sessions(context, workspace)
    const worker = await sessions.create('worker-test', 'worker', 'Complete the public task and verify your work.')
    const router = await sessions.create('router-test', 'router', 'Route the request; the worker owns technical planning.')
    assert.equal(worker.session.header.cwd, workspace)
    assert.equal(router.session.header.cwd, workspace)
    // Inspect actual registered tools after native preset activation.
    const names = worker.ctx.tools.schemas(scopeOf(worker.ctx)).map(tool => tool.name)
    for (const name of ['read', 'write', 'edit', 'glob', 'grep', 'bash', 'todo_write',
      'subagent', 'list_subagent_models', 'send_message', 'list_agents', 'interrupt_agent', 'job_output', 'job_list', 'job_kill']) {
      assert.ok(names.includes(name), `worker is missing ${name}: ${names.join(', ')}`)
    }
    const routerNames = router.ctx.tools.schemas(scopeOf(router.ctx)).map(tool => tool.name)
    assert.ok(!routerNames.includes('subagent'))
    assert.ok(!routerNames.includes('write'))
    const modules = context.agentPresets.inspectCompositions(worker.ctx).flatMap(item => item.modules.map(row => row.moduleName))
    assert.ok(modules.some(name => name.includes('/dsh-compaction-basic/')))
    assert.ok(modules.some(name => name.includes('/dsh-compaction-tool-result-pruner/')))

    await sessions.send(worker, 'initial', 'Start the delegated check.')
    await quiescent(context, worker)
    const children = await context.subagents.listChildren(worker.id)
    assert.equal(children.length, 1, JSON.stringify(worker.session.snapshotEvents()))
    assert.equal(children[0].id, childId)
    assert.equal(children[0].mode, 'continuable')
    {
      await using child = await context.sessionPersistence.open(childId, 'read')
      assert.equal(child.header.cwd, workspace)
    }
    assert.ok(JSON.stringify(worker.session.snapshotEvents()).includes('First finding confirmed.'))

    await sessions.send(worker, 'followup', 'CONTINUE_CHILD: ask the same child for another check.')
    await quiescent(context, worker)
    assert.equal((await context.subagents.listChildren(worker.id)).length, 1)
    assert.ok(JSON.stringify(worker.session.snapshotEvents()).includes('Second finding confirmed.'))
    const childRequests = adapter.requests.filter(request => request.sessionId === childId)
    for (const request of adapter.requests) {
      assert.ok(request.messages.some(message => message.role === 'system'
        && JSON.stringify(message).includes('rather than build-background or check-background')),
      'Workers and native children must receive the same managed-job guidance')
    }
    assert.ok(childRequests.at(-1).messages.some(message => JSON.stringify(message).includes('First finding confirmed.')),
      'Continuing a child must preserve its earlier conversation')
    assert.ok(childPresets.length >= 2 && childPresets.every(preset => preset === 'standard'), JSON.stringify(childPresets))
    assert.ok(childTimeouts.every(timeout => timeout === 60_000), JSON.stringify(childTimeouts))
    assert.equal(context.agentPresets.serviceFor(worker, 'shell').config.timeoutMs.get(), 5_000)
  } finally {
    await environment.dispose()
  }
})
