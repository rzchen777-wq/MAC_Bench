/** Public benchmark input, kept verbatim and readable after context compaction. */
import type { Request, Ticket } from './tickets.ts'

export interface PublicTask {
  taskId: string
  instruction: string
  interactionInstructions: string
  workspace: string
}

export class TaskSource {
  readonly task: PublicTask
  readonly request: Request

  constructor(task: PublicTask) {
    this.task = structuredClone(task)
    this.request = { id: `task:${task.taskId}`, thread: `benchmark:${task.taskId}`, kind: 'request',
      text: [task.instruction, task.interactionInstructions].filter(Boolean).join('\n\n') }
  }

  candidate(): object {
    return { trigger_id: this.request.id, strength: 'explicit', thread: this.request.thread,
      text: this.task.instruction,
      worker_execution_instructions: this.task.interactionInstructions, related_tickets: [] }
  }

  assignment(ticket: Ticket): string {
    return `Assignment ${ticket.assignment}\nTicket: ${ticket.id}\nWorkspace: ${this.task.workspace}\n`
      + `Original request: trigger/${ticket.triggerId}\n\n${this.request.text}`
  }
}
