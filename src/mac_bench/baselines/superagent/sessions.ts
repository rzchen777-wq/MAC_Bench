/** Native DSH sessions; routing and ticket policy live in our own modules. */
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { boot } from '@deepseek-ai/dsh-app-boot'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { freezeMessage, MessageId } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import type {} from '@deepseek-ai/dsh-permission-presets'
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-session-projection'
import type {} from '@deepseek-ai/dsh-tools'
import { PRESETS, profile, type ProfileOptions, type Role } from './profile.ts'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'mac-superagent': { kind: 'mac-superagent'; form: 'relay' }
  }
}

export async function startHarness(options: ProfileOptions): Promise<Context> {
  await mkdir(options.stateDirectory, { recursive: true })
  // One episode owns this runner process. Workspace instructions and skills
  // must not inherit the operator's ~/.dsh or ~/.agents configuration.
  process.env.DSH_HOME = options.stateDirectory
  process.env.DSH_AGENTS_HOME = join(options.stateDirectory, 'agents')
  const config = join(options.stateDirectory, 'harness.json')
  await writeFile(config, JSON.stringify(profile(options), null, 2) + '\n')
  // A generated configuration lives in episode artifacts. Package resolution
  // remains relative to our source, never to a downloaded prototype checkout.
  const context = await boot('mac-bench-superagent', config, [], undefined, import.meta.url)
  // DSH's current spawn provider joins the parent's preset. Superagent gives
  // children standard's longer foreground window instead. Apply it in native
  // initialization, before any queued work, including a cold continuation.
  context.on('agent/created', async ({ agent }) => {
    if (agent.session.header.origin === 'subagent') {
      await context.agentPresets.mount(agent.ctx, PRESETS.subagent)
      if (context.sessionProjections.stateOf(agent.session, 'agentPreset') !== PRESETS.subagent) {
        agent.session.append('agent-preset/selected', { agentPreset: PRESETS.subagent })
      }
    }
    agent.ctx.systemPrompt.section({ name: 'mac-execution', order: 9998, interpolate: false,
      text: 'Run build and test commands directly with bash. When the task provides develop.sh, '
        + 'use its build or check command, rather than build-background or check-background. '
        + 'Bash already keeps long-running commands as managed jobs and delivers completion notifications. '
        + 'Use the returned job handle to inspect output or stop the command. A test process already running '
        + 'does not automatically reload later source edits; run a new check to validate the edited code.',
    })
  })
  return context
}

export class Sessions {
  readonly context: Context
  readonly workspace: string
  private readonly delivered = new Map<string, Set<string>>()

  constructor(context: Context, workspace: string) {
    this.context = context
    this.workspace = workspace
  }

  async create(id: string, role: Role, instructions: string): Promise<Agent> {
    const ctx = this.context
    // Programmatic agents keep cwd in session metadata. The desktop session
    // controller creates host directories and is unsuitable for remote worlds.
    const handle = await ctx.agents.create({
      sessionId: SessionId(id), meta: { cwd: this.workspace, agentPreset: PRESETS[role] },
      agentOptions: ctx.agentDefaultModel.currentSelection(),
      setup: async (agentCtx, agent) => {
        await ctx.agentPresets.mount(agentCtx, PRESETS[role])
        ctx.permissionPresets.set(agent.session, 'danger-full-access')
        agentCtx.systemPrompt.section({ name: 'superagent-role', order: 9999,
          interpolate: false, text: `Session: ${id}\nRole: ${role}\n\n${instructions}`,
          ...(role === 'router' ? { complete: true } : {}),
        })
        if (role === 'router') agentCtx.tools.restrict({
          allow: ['skill', 'bash', 'read', 'read_image', 'job_output', 'job_list', 'job_kill'],
        })
      },
    })
    ctx.effect(() => () => handle.dispose())
    return handle.agent
  }

  /** A stable delivery ID prevents repeated status or assignment messages. */
  async send(agent: Agent, id: string, text: string, mode: 'queue' | 'steer' = 'steer'): Promise<void> {
    let ids = this.delivered.get(agent.id)
    if (!ids) this.delivered.set(agent.id, ids = new Set())
    if (ids.has(id)) return
    const message = freezeMessage({ id: MessageId(`mac-superagent:${id}`), role: 'user' as const,
      content: [{ type: 'text' as const, text }], source: { kind: 'mac-superagent' as const, form: 'relay' as const },
    })
    if (mode === 'queue') agent.followup(message)
    else agent.steer(message)
    ids.add(id)
    await this.context.sessionPersistence.flush()
  }
}
