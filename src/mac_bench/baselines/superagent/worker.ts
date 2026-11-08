/** Worker scheduling around native sessions, child agents and background jobs. */
import { readFile } from 'node:fs/promises'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-jobs'
import type {} from '@deepseek-ai/dsh-subagent'
import { z } from 'zod'
import { readTool, register } from './tools.ts'
import { Sessions } from './sessions.ts'
import { Tickets, type Ticket } from './tickets.ts'
import { TaskSource } from './source.ts'

interface Work {
  agent: Agent
  settling?: AbortController
  joining?: Promise<boolean>
  stopping?: Promise<void>
}

export class Workers {
  readonly sessions: Sessions
  readonly tickets: Tickets
  readonly source: TaskSource
  private readonly notify: (ticket: Ticket) => Promise<void>
  private readonly fail: (error: unknown) => void
  private readonly backgroundMs: number
  private readonly work = new Map<string, Work>()
  private readonly launching = new Map<string, Promise<Work>>()
  private readonly inputs = new Map<string, { ticketId: string; revision: number }>()
  private readonly statusRequests = new Set<string>()
  private readonly pending = new Set<Promise<void>>()
  private closed = false

  constructor(sessions: Sessions, tickets: Tickets, source: TaskSource,
    notify: (ticket: Ticket) => Promise<void>, fail: (error: unknown) => void, backgroundMs = 1_800_000) {
    this.sessions = sessions; this.tickets = tickets; this.source = source
    this.notify = notify; this.fail = fail; this.backgroundMs = backgroundMs
    const ctx = sessions.context
    ctx.on('session/event', (_session, event) => {
      if (event.type !== 'user/message') return
      const input = this.inputs.get(event.data.id)
      if (input) tickets.read(input.ticketId, input.revision)
    })
    ctx.on('agent/status', ({ agent, status }) => {
      for (const [id, work] of this.work) {
        if (agent !== work.agent || this.closed || work.stopping) continue
        work.settling?.abort()
        if (status === 'idle') {
          const pending = this.settle(id, work)
          this.pending.add(pending)
          pending.catch(this.fail).finally(() => this.pending.delete(pending))
        }
        else tickets.started(id)
      }
    })
    ctx.on('agent/error', ({ agent, error }) => {
      for (const [id, work] of this.work) {
        if (agent !== work.agent || this.closed || work.stopping) continue
        const ticket = tickets.get(id)
        if (ticket.status !== 'ended') tickets.report(id, 'failed', String(error))
      }
    })
  }

  async dispatch(ticket: Ticket): Promise<void> {
    if (this.closed || ticket.status === 'ended' || ticket.status === 'handoff') return
    let pending = this.launching.get(ticket.id)
    if (!pending && !this.work.has(ticket.id)) {
      pending = this.create(ticket)
      this.launching.set(ticket.id, pending)
      pending.finally(() => this.launching.delete(ticket.id)).catch(this.fail)
    }
    const work = this.work.get(ticket.id) ?? await pending!
    if (this.closed) return
    work.stopping = undefined
    for (const input of ticket.inputs) {
      const key = `input:${ticket.id}:${input.revision}`
      this.inputs.set(`mac-superagent:${key}`, { ticketId: ticket.id, revision: input.revision })
      const text = input.revision === 1 ? this.source.assignment(ticket)
        : `Follow-up request: trigger/${input.requestId}\n\n${this.tickets.request(input.requestId).text}`
      await this.sessions.send(work.agent, key, text, input.revision === 1 ? 'queue' : 'steer')
    }
  }

  private async create(ticket: Ticket): Promise<Work> {
    const instructions = await readFile(new URL('./prompts/worker.md', import.meta.url), 'utf8')
    const agent = await this.sessions.create(ticket.workerSessionId, 'worker', instructions)
    const work: Work = { agent }
    this.work.set(ticket.id, work)
    readTool(agent, resource => {
      const current = this.tickets.get(ticket.id)
      if (resource === `ticket/${ticket.id}`) return current
      if (resource.startsWith('trigger/') && current.inputs.some(input => input.requestId === resource.slice(8))) {
        return this.tickets.request(resource.slice(8)).text
      }
      throw new Error(`Resource does not belong to this ticket: ${resource}`)
    })
    register(agent, 'orchestrator_report', 'Report progress, checked completion, a concrete blocker, failure, or a request needing no work.',
      z.object({ state: z.enum(['progress', 'ready', 'blocked', 'failed', 'dropped']), summary: z.string().min(1),
        human_action: z.string().min(1).max(1000).nullable().optional() }).strict(),
      async ({ state, summary, human_action }) => {
        const updated = this.tickets.report(ticket.id, state, summary, human_action)
        if (updated.attention) await this.notify(updated)
        return { reported: true, state, ...(updated.attention ? { human_action: updated.attention } : {}) }
      })
    return work
  }

  async requestStatus(ticket: Ticket): Promise<boolean> {
    const work = this.work.get(ticket.id)
    const key = `status:${ticket.id}:${ticket.assignment}:${ticket.readRevision}`
    if (this.closed || !work || ticket.status !== 'waiting' || work.agent.status !== 'idle'
      || this.statusRequests.has(key)) return false
    this.statusRequests.add(key)
    await this.sessions.send(work.agent, key,
      'Your turn ended without a handoff. Report the current status, distinguishing completed work from any remaining work or concrete blocker.')
    return true
  }

  private liveJobs(agent: Agent) {
    return this.sessions.context.jobs.list(agent.id).filter(job => job.owner === agent.id
      && (job.status === 'running' || job.status === 'stopping'))
  }

  /** Observe job completion without consuming the result intended for the model. */
  private jobsFinished(agent: Agent, signal: AbortSignal): Promise<void> {
    if (!this.liveJobs(agent).length || signal.aborted) return Promise.resolve()
    return new Promise(resolve => {
      const finish = () => { unsubscribe(); signal.removeEventListener('abort', finish); resolve() }
      const unsubscribe = this.sessions.context.jobs.events.subscribe({ owner: agent.id }, () => {
        if (!this.liveJobs(agent).length) finish()
      })
      signal.addEventListener('abort', finish, { once: true })
      if (signal.aborted || !this.liveJobs(agent).length) finish()
    })
  }

  private async settle(id: string, work: Work): Promise<void> {
    if (this.tickets.get(id).status === 'handoff') {
      await this.stop(this.tickets.get(id))
      await this.notify(this.tickets.finish(id))
      return
    }
    const look = work.settling = new AbortController()
    const timeout = setTimeout(() => look.abort('background-limit'), this.backgroundMs)
    timeout.unref()
    try {
      // Cache the join: a parent wake interrupts this observation, not its child.
      work.joining ??= this.sessions.context.subagents.waitForChildren(work.agent)
        .finally(() => { work.joining = undefined })
      await new Promise<void>((resolve, reject) => {
        const end = () => { look.signal.removeEventListener('abort', end); resolve() }
        look.signal.addEventListener('abort', end, { once: true })
        work.joining!.then(end, error => { look.signal.removeEventListener('abort', end); reject(error) })
        if (look.signal.aborted) end()
      })
      await this.jobsFinished(work.agent, look.signal)
      if (this.closed || work.stopping || work.agent.status !== 'idle') return
      if (look.signal.aborted && look.signal.reason !== 'background-limit') return
      let detail = ''
      if (look.signal.reason === 'background-limit') {
        const jobs = this.liveJobs(work.agent)
        for (const job of jobs) this.sessions.context.jobs.kill(job.id, work.agent.id, 'Background execution limit reached')
        detail = 'Background execution reached its time limit; unfinished shell jobs were stopped.'
      }
      const current = this.tickets.get(id)
      if (current.status === 'ended' || current.status === 'handoff') return
      const last = work.agent.session.snapshotEvents().filter(event => event.type === 'assistant/message').at(-1)
      const text = last?.type === 'assistant/message'
        ? last.data.message.content.filter(block => block.type === 'text').map(block => block.text).join('\n') : ''
      await this.notify(this.tickets.report(id, 'waiting', [text || current.report?.summary
        || 'Worker ended without a status report.', detail].filter(Boolean).join('\n')))
    } finally {
      clearTimeout(timeout)
      if (work.settling === look) work.settling = undefined
    }
  }

  async stop(ticket: Ticket): Promise<void> {
    const work = this.work.get(ticket.id)
    if (!work) return
    work.settling?.abort()
    work.stopping ??= (async () => {
      const ctx = this.sessions.context
      work.agent.cancel({ kind: 'parent' })
      const children = await ctx.subagents.listChildren(work.agent.id)
      await ctx.subagents.drainChildren(work.agent, children.map(child => child.id))
      const jobs = this.liveJobs(work.agent)
      // Begin collecting before cancellation, so stopped jobs do not wake work.
      const collected = jobs.map(job => ctx.jobs.wait(job.id, 10_000, work.agent.id))
      for (const job of jobs) ctx.jobs.kill(job.id, work.agent.id, 'Assignment ended')
      const settled = await Promise.all(collected)
      if (settled.some(job => job.status === 'running' || job.status === 'stopping')) {
        throw new Error('An assignment process did not stop')
      }
      work.agent.cancel({ kind: 'parent' })
      await work.agent.whenIdle()
    })()
    await work.stopping
  }

  async close(): Promise<void> {
    this.closed = true
    await Promise.allSettled(this.launching.values())
    const results = await Promise.allSettled(this.tickets.list().map(ticket => this.stop(ticket)))
    const errors = results.filter(result => result.status === 'rejected').map(result => result.reason)
    if (errors.length) throw new AggregateError(errors, 'Worker cleanup failed')
  }

  /** Join actual turns and their settling work, including newly woken turns. */
  async wait(): Promise<void> {
    do {
      await Promise.all(this.launching.values())
      await Promise.all([...this.work.values()].map(work => work.agent.whenIdle()))
      await Promise.all(this.pending)
    } while (this.launching.size || this.pending.size || [...this.work.values()].some(work => work.agent.status !== 'idle'))
  }
}
