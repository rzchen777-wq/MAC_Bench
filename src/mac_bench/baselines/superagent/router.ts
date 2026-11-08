/** Superagent's routing role; technical decisions remain with the worker. */
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-jobs'
import { readFile } from 'node:fs/promises'
import { z } from 'zod'
import { register, readTool } from './tools.ts'
import { Sessions } from './sessions.ts'
import { Tickets, type Ticket } from './tickets.ts'
import { TaskSource } from './source.ts'

export interface RouterActions {
  dispatch(ticket: Ticket): Promise<void>
  requestStatus(ticket: Ticket): Promise<boolean>
  stop(ticket: Ticket): Promise<void>
  reply(ticket: Ticket, text: string): void
}

export class Router {
  readonly sessions: Sessions
  readonly tickets: Tickets
  readonly source: TaskSource
  readonly actions: RouterActions
  agent?: Agent
  private readonly lifetime = new AbortController()
  private stopping?: Promise<void>

  constructor(sessions: Sessions, tickets: Tickets, source: TaskSource, actions: RouterActions) {
    this.sessions = sessions; this.tickets = tickets; this.source = source; this.actions = actions
  }

  async start(): Promise<Agent> {
    const instructions = await readFile(new URL('./prompts/router.md', import.meta.url), 'utf8')
    const agent = this.agent = await this.sessions.create('superagent-router', 'router', instructions)
    readTool(agent, resource => {
      if (resource === 'tickets') return this.tickets.list()
      if (resource.startsWith('ticket/')) return this.tickets.get(resource.slice(7))
      if (resource.startsWith('trigger/')) return this.tickets.request(resource.slice(8)).text
      throw new Error(`Unknown resource: ${resource}`)
    })
    register(agent, 'orchestrator_route', 'Route an original request, without adding a technical plan. Each decision is processed independently.',
      z.object({ decisions: z.array(z.object({ kind: z.enum(['accept', 'attach', 'ignore', 'stop']),
        trigger_id: z.string(), ticket_id: z.string().optional(), title: z.string().max(200).optional(),
        note: z.string().max(300).optional(), reason: z.string().optional(),
      }).strict()).min(1).max(20) }).strict(),
      async ({ decisions }) => {
        const results = []
        for (const decision of decisions) {
          try {
            this.tickets.request(decision.trigger_id)
            if (decision.kind === 'ignore') {
              if (!decision.reason?.trim()) throw new Error('ignore requires a reason')
              results.push({ trigger_id: decision.trigger_id, decision: 'ignore', reason: decision.reason })
              continue
            }
            let ticket: Ticket
            if (decision.kind === 'accept') {
              if (!decision.title?.trim()) throw new Error('accept requires a title')
              ticket = this.tickets.accept(decision.trigger_id, decision.title, decision.note)
            } else {
              if (!decision.ticket_id) throw new Error(`${decision.kind} requires ticket_id`)
              ticket = decision.kind === 'stop' ? this.tickets.stop(decision.ticket_id, decision.trigger_id)
                : this.tickets.attach(decision.ticket_id, decision.trigger_id, decision.note)
            }
            if (decision.kind === 'stop') await this.actions.stop(ticket)
            else await this.actions.dispatch(ticket)
            results.push({ trigger_id: decision.trigger_id, decision: decision.kind,
              ticket: this.tickets.get(ticket.id),
              message: decision.kind === 'stop' ? 'The worker has been stopped.'
                : 'The original request is assigned to the independent worker identified by workerSessionId. '
                  + 'It has the task execution tools and reports asynchronously. End your routing turn when no other request needs attention.',
            })
          } catch (error) {
            results.push({ trigger_id: decision.trigger_id, refused: String(error) })
          }
        }
        return { results }
      })
    register(agent, 'orchestrator_ticket_request_status',
      'Ask a waiting worker for a missing status report. Active or handed-off workers are not woken.',
      z.object({ ticket_id: z.string() }).strict(), async ({ ticket_id }) => {
        const requested = await this.actions.requestStatus(this.tickets.get(ticket_id))
        return { requested, ticket: this.tickets.get(ticket_id),
          message: requested ? 'The worker was asked to report its status.'
            : 'No wake-up was needed. The ticket describes the separate worker; its reports arrive asynchronously.' }
      })
    register(agent, 'orchestrator_reply', 'Relay a completed result or concrete human question from the worker report.',
      z.object({ ticket_id: z.string(), text: z.string().min(1) }).strict(), async ({ ticket_id, text }) => {
        const ticket = this.tickets.get(ticket_id)
        if (ticket.status !== 'handoff' && ticket.status !== 'ended') return {
          delivered: false, reason: 'worker_report_pending', ticket,
          message: 'There is no worker handoff to relay yet. Your router tools differ from the worker execution tools. '
            + 'End this routing turn; the worker continues independently and its report will arrive automatically.',
        }
        this.actions.reply(ticket, text)
        return { delivered: true }
      })
    this.tickets.addRequest(this.source.request)
    await this.sessions.send(agent, this.source.request.id, JSON.stringify(this.source.candidate()), 'queue')
    return agent
  }

  async notify(ticket: Ticket, deliveryId: string): Promise<void> {
    if (!this.agent) throw new Error('Router not started')
    if (this.lifetime.signal.aborted) return
    await this.sessions.send(this.agent, deliveryId, JSON.stringify({ kind: 'worker_report',
      from_worker: ticket.workerSessionId, to_router: this.agent.id, ticket }))
  }

  private liveJobs() {
    if (!this.agent) return []
    return this.sessions.context.jobs.list(this.agent.id).filter(job => job.owner === this.agent!.id
      && (job.status === 'running' || job.status === 'stopping'))
  }

  /** A router can inspect public materials with bash too. Its idle turn is
   * not an episode boundary while a managed command can still wake it. */
  async wait(): Promise<void> {
    if (!this.agent) return
    while (!this.lifetime.signal.aborted) {
      await this.agent.whenIdle()
      if (!this.liveJobs().length) return
      await new Promise<void>(resolve => {
        const signal = this.lifetime.signal
        const finish = () => { unsubscribe(); signal.removeEventListener('abort', finish); resolve() }
        const unsubscribe = this.sessions.context.jobs.events.subscribe({ owner: this.agent!.id }, () => {
          if (!this.liveJobs().length) finish()
        })
        signal.addEventListener('abort', finish, { once: true })
        if (signal.aborted || !this.liveJobs().length) finish()
      })
      // Observing events does not collect the result; the model still receives
      // the native completion notification and may route work in its next turn.
    }
  }

  async close(): Promise<void> {
    this.stopping ??= (async () => {
      this.lifetime.abort()
      const agent = this.agent
      if (!agent) return
      agent.cancel({ kind: 'parent' })
      const jobs = this.liveJobs()
      const registry = this.sessions.context.jobs
      const collected = jobs.map(job => registry.wait(job.id, 10_000, agent.id))
      for (const job of jobs) registry.kill(job.id, agent.id, 'Episode ended')
      const settled = await Promise.all(collected)
      agent.cancel({ kind: 'parent' })
      await agent.whenIdle()
      if (settled.some(job => job.status === 'running' || job.status === 'stopping')) {
        throw new Error('A router process did not stop')
      }
    })()
    await this.stopping
  }
}
