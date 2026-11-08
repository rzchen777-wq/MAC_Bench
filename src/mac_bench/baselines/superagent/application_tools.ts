/** Expose the task's existing public application API to workers and children. */
import type { Context } from '@deepseek-ai/cordis'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-tools'
import type {} from './world.ts'

export const name = 'mac-bench-application-tools'
export const inject = ['tools', 'macWorld']

export function apply(ctx: Context, config: { tools: ToolSchema[] }): void {
  for (const tool of config.tools) ctx.effect(() => ctx.tools.register({ ...tool,
    output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: (args, execution) => ctx.macWorld.request('tools.call',
      { name: tool.name, arguments: args }, execution.signal),
  }))
}
