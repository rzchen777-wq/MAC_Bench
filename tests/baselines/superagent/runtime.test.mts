import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { Runtime } from '../../../src/mac_bench/baselines/superagent/runtime.ts'
import { fixture, ScriptedModel } from './support.mts'

test('router dispatches original work; a background job wakes its worker before handoff', { timeout: 20_000 }, async () => {
  const environment = await fixture()
  const { context, workspace, directory } = environment
  const instruction = 'Create result.txt containing done, and verify the written result.'
  const runtime = new Runtime(context, { taskId: 'background', workspace,
    instruction, interactionInstructions: 'Work in the shared directory.' }, join(directory, 'state'))
  let routed = false
  let started = false
  let reported = false
  let replied = false
  let sawCompletionNotice = false
  const adapter = new ScriptedModel(request => {
    if (request.sessionId === 'superagent-router') {
      if (!routed) {
        routed = true
        return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
          trigger_id: 'task:background', title: 'Create the requested result' }] } }
      }
      const ticket = runtime.tickets.list()[0]
      if (ticket?.status === 'ended' && !replied) {
        replied = true
        return { name: 'orchestrator_reply', arguments: { ticket_id: ticket.id, text: 'The result was written and checked.' } }
      }
      return 'The worker owns this task.'
    }
    if (!started) {
      started = true
      return { name: 'bash', arguments: { description: 'Write and check the requested output',
        command: "sleep 0.2; printf 'done\\n' > result.txt; cat result.txt; printf 'BACKGROUND_CHECK_FINISHED\\n'",
        run_in_background: true } }
    }
    sawCompletionNotice ||= request.messages.some(message => message.source?.kind === 'tool-jobs')
    if (sawCompletionNotice && !reported) {
      reported = true
      return { name: 'orchestrator_report', arguments: { state: 'ready', summary: 'Wrote result.txt and checked that it contains done.' } }
    }
    return reported ? 'The requested result is complete.' : 'The command is still running.'
  })
  context.llm.registerAdapter(['fixture'], adapter)
  try {
    const result = await runtime.run()
    assert.equal(result.reason, 'ready', JSON.stringify({ result,
      messages: adapter.requests.at(-1)?.messages.filter(message => message.role !== 'system'),
      worker: adapter.requests.filter(request => request.sessionId !== 'superagent-router').at(-1)?.messages
        .filter(message => message.role !== 'system'),
    }))
    assert.equal(result.tickets.length, 1)
    assert.equal(result.replies.length, 1)
    assert.ok(sawCompletionNotice, 'Completion must be delivered to the same worker session')
    assert.equal(await readFile(join(workspace, 'result.txt'), 'utf8'), 'done\n')
    const workerRequests = adapter.requests.filter(request => request.sessionId !== 'superagent-router')
    assert.ok(workerRequests[0].messages.some(message => JSON.stringify(message).includes(instruction)))
    const worker = context.agents.get(result.tickets[0].workerSessionId)
    const bashCalls = worker.session.snapshotEvents().filter(event => event.type === 'assistant/message')
      .flatMap(event => event.data.message.content).filter(block => block.type === 'tool-call' && block.name === 'bash')
    assert.equal(bashCalls.length, 1, 'Moving a command into the background must not restart it')
    assert.ok(context.jobs.list(worker.id).every(job => !['running', 'stopping'].includes(job.status)))
  } finally {
    await runtime.close()
    await environment.dispose()
  }
})

test('episode shutdown terminates the running command and its child process', { timeout: 20_000 }, async () => {
  const environment = await fixture()
  const { context, workspace, directory } = environment
  const runtime = new Runtime(context, { taskId: 'stop', workspace,
    instruction: 'Inspect the workspace.', interactionInstructions: 'Use the shared workspace.' }, join(directory, 'state'))
  let routed = false
  let launched = false
  const adapter = new ScriptedModel(request => {
    if (request.sessionId === 'superagent-router') {
      if (!routed) {
        routed = true
        return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
          trigger_id: 'task:stop', title: 'Inspect workspace' }] } }
      }
      return 'Work is in progress.'
    }
    if (!launched) {
      launched = true
      return { name: 'bash', arguments: { description: 'Run a long inspection',
        command: "sleep 60 & child=$!; printf '%s\\n' $$ $child > active-pids; wait", run_in_background: true } }
    }
    return 'Waiting for the inspection.'
  })
  context.llm.registerAdapter(['fixture'], adapter)
  const running = runtime.run()
  // Attach before cleanup so a failed assertion cannot hide a rejected run.
  running.catch(() => {})
  try {
    let pids: number[] = []
    for (let i = 0; i < 200; i++) {
      try { pids = (await readFile(join(workspace, 'active-pids'), 'utf8')).trim().split('\n').map(Number); break }
      catch (error) { if (error.code !== 'ENOENT') throw error }
      await delay(10)
    }
    assert.equal(pids.length, 2, 'The long command must actually start before cancellation')
    for (const pid of pids) process.kill(pid, 0)
    await runtime.close()
    await running
    const ticket = runtime.tickets.list()[0]
    assert.ok(context.jobs.list(ticket.workerSessionId).every(job => !['running', 'stopping'].includes(job.status)))
    for (const pid of pids) assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' })
  } finally {
    await runtime.close()
    await environment.dispose()
  }
})

test('router background work is joined before an episode is considered idle', { timeout: 15_000 }, async () => {
  const env = await fixture()
  const runtime = new Runtime(env.context, { taskId: 'router-job', workspace: env.workspace,
    instruction: 'Inspect the request then route it.', interactionInstructions: '' }, join(env.directory, 'state'))
  let launched = false, routed = false, ready = false, received = false
  env.context.llm.registerAdapter(['fixture'], new ScriptedModel(request => {
    if (request.sessionId === 'superagent-router') {
      if (!launched) {
        launched = true
        return { name: 'bash', arguments: { description: 'Inspect public routing material',
          command: 'sleep .3; printf ready', run_in_background: true } }
      }
      received ||= request.messages.some(message => message.source?.kind === 'tool-jobs')
      if (received && !routed) {
        routed = true
        return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
          trigger_id: 'task:router-job', title: 'Complete request' }] } }
      }
      return 'Waiting for the existing work.'
    }
    if (!ready) {
      ready = true
      return { name: 'orchestrator_report', arguments: { state: 'ready', summary: 'Completed.' } }
    }
    return 'Done.'
  }))
  try {
    const result = await runtime.run()
    assert.equal(result.reason, 'ready', JSON.stringify(result))
    assert.ok(received && routed && ready)
  } finally { await runtime.close(); await env.dispose() }
})

test('worker model error is a failed handoff rather than an idle unfinished ticket', { timeout: 15_000 }, async () => {
  const env = await fixture()
  const runtime = new Runtime(env.context, { taskId: 'failed-worker', workspace: env.workspace,
    instruction: 'Complete request.', interactionInstructions: '' }, join(env.directory, 'state'))
  let routed = false
  env.context.llm.registerAdapter(['fixture'], new ScriptedModel(request => {
    if (request.sessionId !== 'superagent-router') throw new Error('fixture worker failure')
    if (!routed) {
      routed = true
      return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
        trigger_id: 'task:failed-worker', title: 'Complete request' }] } }
    }
    return 'Observed worker status.'
  }))
  try {
    const result = await runtime.run()
    assert.equal(result.reason, 'failed', JSON.stringify(result))
    assert.equal(result.tickets[0].outcome, 'failed')
  } finally { await runtime.close(); await env.dispose() }
})

test('shutdown drains router-owned jobs as well as worker jobs', { timeout: 15_000 }, async () => {
  const env = await fixture()
  const runtime = new Runtime(env.context, { taskId: 'router-stop', workspace: env.workspace,
    instruction: 'Inspect public material.', interactionInstructions: '' }, join(env.directory, 'state'))
  let launched = false
  env.context.llm.registerAdapter(['fixture'], new ScriptedModel(() => {
    if (launched) return 'Waiting for the inspection.'
    launched = true
    return { name: 'bash', arguments: { description: 'Inspect material', run_in_background: true,
      command: "printf '%s' $$ > router-pid; exec sleep 60" } }
  }))
  const running = runtime.run()
  running.catch(() => {})
  try {
    let pid = 0
    for (let i = 0; i < 200; i++) {
      try { pid = Number(await readFile(join(env.workspace, 'router-pid'), 'utf8')); break }
      catch (error) { if (error.code !== 'ENOENT') throw error }
      await delay(10)
    }
    assert.ok(pid > 0)
    await runtime.close()
    await running
    assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' })
    assert.ok(env.context.jobs.list(runtime.router.agent!.id).every(job => !['running', 'stopping'].includes(job.status)))
  } finally { await runtime.close(); await env.dispose() }
})

test('a production worker delegates without leaking its ticket-control tools into the child', { timeout: 15_000 }, async () => {
  const env = await fixture()
  const runtime = new Runtime(env.context, { taskId: 'child-scope', workspace: env.workspace,
    instruction: 'Delegate a check then report.', interactionInstructions: '' }, join(env.directory, 'state'))
  let routed = false, delegated = false, reported = false, childSeen = false
  env.context.llm.registerAdapter(['fixture'], new ScriptedModel(request => {
    if (request.sessionId === 'superagent-router') {
      if (routed) return 'Waiting for the worker.'
      routed = true
      return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
        trigger_id: 'task:child-scope', title: 'Delegate check' }] } }
    }
    if (!request.sessionId.startsWith('worker-')) {
      childSeen = true
      const tools = request.tools.map(tool => tool.name)
      assert.ok(tools.includes('bash') && tools.includes('send_message'))
      assert.ok(!tools.some(name => name.startsWith('orchestrator_')), JSON.stringify(tools))
      return 'The delegated check is complete.'
    }
    if (!delegated) {
      delegated = true
      return { name: 'subagent', arguments: { description: 'Run delegated check', prompt: 'Report that your check is complete.' } }
    }
    if (childSeen && request.messages.some(message => message.source?.kind === 'subagent-settled') && !reported) {
      reported = true
      return { name: 'orchestrator_report', arguments: { state: 'ready', summary: 'Received the delegated check.' } }
    }
    return 'Waiting for the existing child.'
  }))
  try {
    const result = await runtime.run()
    assert.equal(result.reason, 'ready', JSON.stringify(result))
    assert.ok(childSeen && reported)
  } finally { await runtime.close(); await env.dispose() }
})


test('router sees an independent worker and an early reply cannot replace its eventual report', { timeout: 20_000 }, async () => {
  const env = await fixture()
  const runtime = new Runtime(env.context, { taskId: 'role-boundary', workspace: env.workspace,
    instruction: 'Complete the public task.', interactionInstructions: 'Use call_tool to update business records.' }, join(env.directory, 'state'))
  let routed = false, earlyReply = false, released = false, started = false, reported = false, relayed = false
  const adapter = new ScriptedModel(request => {
    const history = JSON.stringify(request.messages)
    if (request.sessionId === 'superagent-router') {
      assert.ok(history.includes('Session: superagent-router'))
      assert.ok(!request.tools.some(tool => tool.name === 'orchestrator_report'))
      if (!routed) {
        assert.ok(history.includes('worker_execution_instructions'))
        routed = true
        return { name: 'orchestrator_route', arguments: { decisions: [{ kind: 'accept',
          trigger_id: 'task:role-boundary', title: 'Complete the task' }] } }
      }
      const ticket = runtime.tickets.list()[0]
      assert.notEqual(ticket.workerSessionId, request.sessionId)
      if (!earlyReply) {
        assert.ok(history.includes('independent worker'))
        earlyReply = true
        return { name: 'orchestrator_reply', arguments: { ticket_id: ticket.id, text: 'My router has no business tools.' } }
      }
      if (!released) {
        assert.ok(history.includes('worker_report_pending'))
        assert.equal(runtime.tickets.get(ticket.id).outcome, undefined)
        writeFileSync(join(env.workspace, 'release-worker'), 'continue')
        released = true
      }
      if (ticket.status === 'ended' && !relayed) {
        assert.ok(history.includes('from_worker') && history.includes('to_router'))
        relayed = true
        return { name: 'orchestrator_reply', arguments: { ticket_id: ticket.id, text: 'The worker completed its check.' } }
      }
      return 'The independent worker continues; its report will arrive here.'
    }
    assert.ok(history.includes('Role: worker'))
    assert.ok(history.includes('Use call_tool to update business records.'))
    if (!started) {
      started = true
      return { name: 'bash', arguments: { description: 'Keep the worker active until the router finishes its turn',
        command: 'while [ ! -e release-worker ]; do sleep .01; done; printf checked > result.txt', run_in_background: true } }
    }
    if (released && request.messages.some(message => message.source?.kind === 'tool-jobs') && !reported) {
      reported = true
      return { name: 'orchestrator_report', arguments: { state: 'ready', summary: 'Checked the public task.' } }
    }
    return 'The check is running.'
  })
  env.context.llm.registerAdapter(['fixture'], adapter)
  try {
    const result = await runtime.run()
    assert.equal(result.reason, 'ready')
    assert.equal(result.replies.length, 1)
    assert.equal(result.replies[0].text, 'The worker completed its check.')
    assert.equal(await readFile(join(env.workspace, 'result.txt'), 'utf8'), 'checked')
    assert.ok(earlyReply && released && reported && relayed)
  } finally { await runtime.close(); await env.dispose() }
})
