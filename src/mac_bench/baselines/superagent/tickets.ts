/** Request, assignment and handoff state for a benchmark episode. */
import { randomUUID } from 'node:crypto'

export type ReportState = 'progress' | 'ready' | 'blocked' | 'failed' | 'dropped' | 'waiting'
export interface Request {
  id: string
  text: string
  thread: string
  kind: 'request' | 'followup' | 'stop'
}
export interface Report {
  state: ReportState
  summary: string
  humanAction?: string | null
  inputRevision: number
}
export interface Ticket {
  id: string
  title: string
  triggerId: string
  workerSessionId: string
  assignment: number
  inputRevision: number
  readRevision: number
  inputs: { requestId: string; revision: number; note?: string }[]
  status: 'queued' | 'working' | 'waiting' | 'handoff' | 'ended'
  report?: Report
  outcome?: 'ready' | 'blocked' | 'failed' | 'dropped' | 'stopped'
  attention?: string
}
export interface TicketEvent {
  kind: string
  ticket: Ticket
  request?: Request
}

/** One state owner; background work belongs to DSH and is observed by worker.ts. */
export class Tickets {
  private readonly records = new Map<string, Ticket>()
  private readonly requests = new Map<string, Request>()
  private readonly decisions = new Map<string, string>()
  private readonly emit: (event: TicketEvent) => void

  constructor(emit: (event: TicketEvent) => void) { this.emit = emit }

  addRequest(request: Request): void {
    const previous = this.requests.get(request.id)
    if (previous && JSON.stringify(previous) !== JSON.stringify(request)) throw new Error('Request ID already has different content')
    this.requests.set(request.id, structuredClone(request))
  }

  request(id: string): Request {
    const request = this.requests.get(id)
    if (!request) throw new Error(`Unknown request: ${id}`)
    return structuredClone(request)
  }

  get(id: string): Ticket { return structuredClone(this.mutable(id)) }
  list(): Ticket[] { return [...this.records.values()].map(ticket => structuredClone(ticket)) }

  accept(triggerId: string, title: string, note?: string): Ticket {
    const decided = this.decisions.get(triggerId)
    if (decided) return this.get(decided)
    const request = this.request(triggerId)
    if (request.kind === 'stop') throw new Error('A stop request cannot authorize new work')
    const id = randomUUID()
    const ticket: Ticket = { id, title, triggerId, workerSessionId: `worker-${id}`, assignment: 1,
      inputRevision: 1, readRevision: 0, inputs: [{ requestId: triggerId, revision: 1, ...(note ? { note } : {}) }], status: 'queued' }
    this.records.set(id, ticket)
    this.decisions.set(triggerId, id)
    this.changed('accepted', ticket, request)
    return this.get(id)
  }

  attach(id: string, requestId: string, note?: string): Ticket {
    const ticket = this.mutable(id)
    const request = this.request(requestId)
    if (request.kind === 'stop') throw new Error('Use stop for a stop request')
    if (ticket.outcome === 'stopped') throw new Error('Stopped work requires a new request and ticket')
    if (ticket.inputs.some(input => input.requestId === requestId)) return this.get(id)
    const owner = this.decisions.get(requestId)
    if (owner && owner !== id) throw new Error(`Request already belongs to ${owner}`)
    ticket.inputRevision += 1
    ticket.inputs.push({ requestId, revision: ticket.inputRevision, ...(note ? { note } : {}) })
    if (ticket.status === 'ended' || ticket.status === 'handoff') ticket.assignment += 1
    ticket.status = 'queued'
    delete ticket.outcome
    delete ticket.attention
    this.decisions.set(requestId, id)
    this.changed('input', ticket, request)
    return this.get(id)
  }

  started(id: string): void {
    const ticket = this.mutable(id)
    if (ticket.status === 'ended' || ticket.status === 'handoff') return
    ticket.status = 'working'
    this.changed('working', ticket)
  }

  /** Called when DSH commits a delivered request into the worker's history. */
  read(id: string, revision: number): void {
    const ticket = this.mutable(id)
    if (revision < 1 || revision > ticket.inputRevision) throw new Error('Invalid input revision')
    ticket.readRevision = Math.max(ticket.readRevision, revision)
    this.changed('input_read', ticket)
  }

  report(id: string, state: ReportState, summary: string, humanAction?: string | null): Ticket {
    const ticket = this.mutable(id)
    if (ticket.status === 'ended') throw new Error('Ticket ended')
    ticket.report = { state, summary, inputRevision: ticket.readRevision,
      ...(humanAction !== undefined ? { humanAction } : {}) }
    if (humanAction === null || (state === 'ready' && humanAction === undefined)) delete ticket.attention
    else if (humanAction) ticket.attention = humanAction
    const unread = ticket.readRevision < ticket.inputRevision
    if (state === 'blocked' || state === 'failed') ticket.status = 'handoff'
    else if (!unread && (ticket.attention || state === 'ready' || state === 'dropped')) ticket.status = 'handoff'
    else if (!unread && state === 'waiting') ticket.status = 'waiting'
    else ticket.status = 'working'
    this.changed('report', ticket)
    return this.get(id)
  }

  /** Called only after the worker and its background work have been drained. */
  finish(id: string): Ticket {
    const ticket = this.mutable(id)
    if (ticket.status !== 'handoff') throw new Error('Ticket has no pending handoff')
    ticket.status = 'ended'
    const state = ticket.report?.state
    ticket.outcome = ticket.attention ? 'blocked'
      : state === 'ready' || state === 'dropped' || state === 'failed' ? state : 'blocked'
    this.changed('ended', ticket)
    return this.get(id)
  }

  stop(id: string, requestId: string): Ticket {
    if (this.request(requestId).kind !== 'stop') throw new Error('Stopping work requires an explicit stop request')
    const ticket = this.mutable(id)
    ticket.status = 'ended'
    ticket.outcome = 'stopped'
    this.changed('ended', ticket)
    return this.get(id)
  }

  private mutable(id: string): Ticket {
    const ticket = this.records.get(id)
    if (!ticket) throw new Error(`Unknown ticket: ${id}`)
    return ticket
  }
  private changed(kind: string, ticket: Ticket, request?: Request): void {
    this.emit({ kind, ticket: structuredClone(ticket), ...(request ? { request: structuredClone(request) } : {}) })
  }
}
