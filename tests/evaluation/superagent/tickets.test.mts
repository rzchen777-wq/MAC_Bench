import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Tickets } from '../../../src/mac_bench/baselines/superagent/tickets.ts'
import { TaskSource } from '../../../src/mac_bench/baselines/superagent/source.ts'

test('routing retries do not duplicate work or rewrite the original request', () => {
  const source = new TaskSource({ taskId: 'sample', instruction: 'Implement A and preserve B.\nRead /work/TASK.md.',
    interactionInstructions: 'Use the public tools.', workspace: '/work/project' })
  const events = []
  const tickets = new Tickets(event => events.push(event))
  tickets.addRequest(source.request)
  const first = tickets.accept(source.request.id, 'Implement A')
  const duplicate = tickets.accept(source.request.id, 'Add invented requirement C')
  assert.deepEqual(first, duplicate)
  assert.equal(events.filter(event => event.kind === 'accepted').length, 1)
  assert.ok(source.assignment(first).includes(source.request.text))
  assert.deepEqual(source.candidate(), { trigger_id: source.request.id, strength: 'explicit',
    thread: source.request.thread, text: source.task.instruction,
    worker_execution_instructions: source.task.interactionInstructions, related_tickets: [] })
  assert.equal('sessionId' in first, false)
  assert.ok(!source.assignment(first).includes('invented requirement'))
})

test('a ready report cannot drop a follow-up that the worker has not read', () => {
  const tickets = new Tickets(() => {})
  tickets.addRequest({ id: 'one', kind: 'request', thread: 'task', text: 'Implement A.' })
  const ticket = tickets.accept('one', 'A')
  tickets.started(ticket.id)
  tickets.read(ticket.id, 1)
  tickets.addRequest({ id: 'two', kind: 'followup', thread: 'task', text: 'Also preserve B.' })
  tickets.attach(ticket.id, 'two')
  assert.equal(tickets.report(ticket.id, 'ready', 'A is implemented.').status, 'working')
  assert.throws(() => tickets.finish(ticket.id), /no pending handoff/)
  tickets.read(ticket.id, 2)
  assert.equal(tickets.report(ticket.id, 'ready', 'A is implemented; B is preserved.').status, 'handoff')
  assert.equal(tickets.finish(ticket.id).outcome, 'ready')
})

test('a blocker is a handoff, not authorization to stop or invent an answer', () => {
  const tickets = new Tickets(() => {})
  tickets.addRequest({ id: 'one', kind: 'request', thread: 'task', text: 'Resolve the request.' })
  const ticket = tickets.accept('one', 'Resolve')
  tickets.read(ticket.id, 1)
  const report = tickets.report(ticket.id, 'progress', 'Approval is needed.', 'Which account is authorized?')
  assert.equal(report.status, 'handoff')
  assert.throws(() => tickets.stop(ticket.id, 'one'), /explicit stop/)
  assert.equal(tickets.finish(ticket.id).outcome, 'blocked')
  tickets.addRequest({ id: 'answer', kind: 'followup', thread: 'task', text: 'Use account X.' })
  const resumed = tickets.attach(ticket.id, 'answer')
  assert.equal(resumed.workerSessionId, ticket.workerSessionId)
  assert.equal(resumed.assignment, 2)
  assert.equal(resumed.attention, undefined)
})

test('a question cannot discard queued input, and a ready report clears the resolved question', () => {
  const tickets = new Tickets(() => {})
  tickets.addRequest({ id: 'one', kind: 'request', thread: 'task', text: 'Update the approved account.' })
  const ticket = tickets.accept('one', 'Update account')
  tickets.read(ticket.id, 1)
  tickets.addRequest({ id: 'two', kind: 'followup', thread: 'task', text: 'The approved account is X.' })
  tickets.attach(ticket.id, 'two')
  assert.equal(tickets.report(ticket.id, 'progress', 'Checking authorization.', 'Which account?').status, 'working')
  assert.throws(() => tickets.finish(ticket.id), /no pending handoff/)
  tickets.read(ticket.id, 2)
  const ready = tickets.report(ticket.id, 'ready', 'Updated account X and verified the result.')
  assert.equal(ready.attention, undefined)
  assert.equal(tickets.finish(ticket.id).outcome, 'ready')
})
