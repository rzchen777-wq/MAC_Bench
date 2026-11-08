/** The superagent orchestration owned by MAC-Bench, using DSH for execution. */
import { appendFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { Sessions } from './sessions.ts'
import { TaskSource, type PublicTask } from './source.ts'
import { Tickets, type Ticket } from './tickets.ts'
import { Router } from './router.ts'
import { Workers } from './worker.ts'

export interface RuntimeResult {
  reason: 'ready' | 'blocked' | 'failed' | 'dropped' | 'stopped' | 'waiting' | 'not_routed'
  tickets: Ticket[]
  replies: { ticketId: string; text: string }[]
}

export class Runtime {
  readonly sessions: Sessions
  readonly source: TaskSource
  readonly tickets: Tickets
  readonly workers: Workers
  readonly router: Router
  private sequence = 0
  private failure?: unknown
  private readonly replies: RuntimeResult['replies'] = []
  private stopping?: Promise<void>

  constructor(context: Context, task: PublicTask, stateDirectory: string, backgroundMs = 1_800_000) {
    const event = (kind: string, data: unknown) => {
      appendFileSync(join(stateDirectory, 'orchestration.jsonl'), JSON.stringify({
        seq: ++this.sequence, time: Date.now(), kind, data,
      }) + '\n')
    }
    this.sessions = new Sessions(context, task.workspace)
    this.source = new TaskSource(task)
    this.tickets = new Tickets(update => event('ticket', update))
    this.workers = new Workers(this.sessions, this.tickets, this.source,
      ticket => this.router.notify(ticket, `report:${ticket.id}:${++this.sequence}`),
      error => { this.failure = error; event('error', String(error)) }, backgroundMs)
    this.router = new Router(this.sessions, this.tickets, this.source, {
      dispatch: ticket => this.workers.dispatch(ticket), requestStatus: ticket => this.workers.requestStatus(ticket),
      stop: ticket => this.workers.stop(ticket),
      reply: (ticket, text) => { this.replies.push({ ticketId: ticket.id, text }); event('reply', { ticketId: ticket.id, text }) },
    })
    context.on('agent/error', ({ agent, error }) => {
      if (agent === this.router.agent) { this.failure = error; event('error', String(error)) }
    })
  }

  async run(): Promise<RuntimeResult> {
    await this.router.start()
    for (;;) {
      await this.router.wait()
      await this.workers.wait()
      const before = this.sequence
      await this.router.wait()
      if (this.failure) throw this.failure
      // A report can wake the router, which can dispatch another turn. Observe
      // that work before deciding the episode has nothing left to execute.
      if (before !== this.sequence) continue
      const tickets = this.tickets.list()
      const reason = !tickets.length ? 'not_routed'
        : tickets.some(ticket => ticket.status !== 'ended') ? 'waiting'
        : tickets.some(ticket => ticket.outcome === 'failed') ? 'failed'
        : tickets.some(ticket => ticket.outcome === 'blocked') ? 'blocked'
        : tickets.some(ticket => ticket.outcome === 'stopped') ? 'stopped'
        : tickets.every(ticket => ticket.outcome === 'dropped') ? 'dropped' : 'ready'
      return { reason, tickets, replies: [...this.replies] }
    }
  }

  async close(): Promise<void> {
    this.stopping ??= (async () => {
      const closed = await Promise.allSettled([this.router.close(), this.workers.close()])
      await this.sessions.context.sessionPersistence.flush()
      const errors = closed.filter(value => value.status === 'rejected').map(value => value.reason)
      if (errors.length) throw new AggregateError(errors, 'Superagent cleanup failed')
    })()
    await this.stopping
  }
}
