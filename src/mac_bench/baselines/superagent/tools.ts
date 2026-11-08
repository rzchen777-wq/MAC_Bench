/** Model-facing orchestration tools, separate from DSH's work tools. */
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-tools'
import { z } from 'zod'

export function register<S extends z.ZodType>(agent: Agent, name: string, description: string,
  schema: S, execute: (args: z.infer<S>, signal: AbortSignal) => Promise<unknown>): void {
  agent.ctx.effect(() => agent.ctx.tools.register({ name, description, parameters: z.toJSONSchema(schema),
    output: { schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: (args, execution) => execute(schema.parse(args), execution.signal),
  }))
}

export function readTool(agent: Agent, read: (resource: string) => unknown): void {
  register(agent, 'orchestrator_read',
    'Read original requests or ticket records. Resources: tickets, ticket/<id>, trigger/<id>. Follow next_offset for long text.',
    z.object({ resource: z.string().min(1), offset: z.number().int().min(0).default(0),
      limit: z.number().int().min(1).max(8000).default(4000) }).strict(),
    async ({ resource, offset, limit }) => {
      const value = read(resource)
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
      return { resource, text: text.slice(offset, offset + limit), total: text.length,
        next_offset: offset + limit < text.length ? offset + limit : null }
    })
}
