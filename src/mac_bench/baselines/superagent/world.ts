/** Connection from native DSH capabilities to the public MAC-Bench world. */
import { AsyncLocalStorage } from 'node:async_hooks'
import { appendFileSync } from 'node:fs'
import { Context, Service } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-tools'
import type { ToolSchema } from '@deepseek-ai/dsh-llm'
import type { WorldProviders } from './profile.ts'

interface Identity { agent_id?: string; tool_call_id?: string; tool?: string }
interface Config { url: string; tokenEnv: string; workspace: string; timingFile?: string }
interface TimingScope { span: number; agent_id?: string; phase: string }

/** All role capabilities resolve into the same task execution world. */
export function worldProviders(config: Config, tools: ToolSchema[] = []): WorldProviders {
  return {
    filesystem: { id: 'filesystem', name: new URL('./filesystem.ts', import.meta.url).href },
    subprocess: { id: 'subprocess', name: new URL('./subprocess.ts', import.meta.url).href },
    services: [{ id: 'world', name: import.meta.url, config },
      { id: 'shell-env', name: '@deepseek-ai/dsh-shell-env' }],
    shell: foregroundMs => ({ id: 'shell', name: new URL('./shell.ts', import.meta.url).href, config: {
      cwd: config.workspace, timeoutMs: foregroundMs, maxTimeoutMs: foregroundMs,
    } }),
    tools: tools.length ? [{ id: 'application-tools', name: new URL('./application_tools.ts', import.meta.url).href,
      config: { tools } }] : [],
  }
}

export class WorldError extends Error {
  readonly code: string
  constructor(code: string, message: string) { super(message); this.code = code }
}

declare module '@deepseek-ai/cordis' {
  interface Context { macWorld: World }
}

export class World extends Service {
  static inject = ['tools']
  readonly workspace: string
  private readonly url: string
  private readonly credential: string
  private readonly identity = new AsyncLocalStorage<Identity>()
  private readonly timingScope = new AsyncLocalStorage<TimingScope>()
  private readonly timingFile?: string
  private sequence = 0

  constructor(ctx: Context, config: Config) {
    super(ctx, 'macWorld')
    this.workspace = config.workspace
    this.url = config.url
    this.timingFile = config.timingFile
    const credential = process.env[config.tokenEnv]
    if (!credential) throw new Error(`Missing world connection credential: ${config.tokenEnv}`)
    this.credential = credential
    ctx.on('agent/pre-step', ({ agent }, next) => this.measure('pre_step', { agent_id: agent.id }, next),
      { prepend: true })
    ctx.on('tools/execute', (execution, next) => this.identity.run({
      agent_id: execution.agent?.id, tool_call_id: execution.callId, tool: execution.name,
    }, () => this.measure('tool', { agent_id: execution.agent?.id, tool: execution.name,
      tool_call_id: execution.callId }, async () => {
      if (!['read', 'read_image', 'write', 'edit', 'glob', 'grep', 'bash'].includes(execution.name)) return next()
      await this.request('tool.start', execution.arguments, execution.signal)
      let result
      try {
        result = await next()
      } catch (error) {
        await this.request('tool.end', { isError: true })
        throw error
      }
      await this.request('tool.end', { isError: result.isError })
      return result
    })))
  }

  private trace(row: object): void {
    if (this.timingFile) appendFileSync(this.timingFile, JSON.stringify(row) + '\n')
  }

  async measure<T>(phase: string, details: { agent_id?: string; [key: string]: unknown },
    action: () => Promise<T>): Promise<T> {
    const span = ++this.sequence, parent_span = this.timingScope.getStore()?.span
    const started_at_ms = Date.now(), started = performance.now()
    let status = 'completed'
    try {
      return await this.timingScope.run({ span, phase, agent_id: details.agent_id }, action)
    } catch (error) { status = 'error'; throw error }
    finally {
      this.trace({ kind: 'span', span, parent_span, phase, ...details, started_at_ms,
        duration_ms: performance.now() - started, status })
    }
  }

  async request<T>(operation: string, args: unknown, signal?: AbortSignal): Promise<T> {
    const started_at_ms = Date.now(), started = performance.now()
    let status = 'completed'
    try {
      const response = await fetch(this.url, { method: 'POST', signal,
        headers: { 'content-type': 'application/json', 'authorization': `Bearer ${this.credential}` },
        body: JSON.stringify({ operation, arguments: args, identity: this.identity.getStore() ?? {} }),
      })
      if (!response.ok) throw new WorldError('WORLD_TRANSPORT', `World connection returned HTTP ${response.status}`)
      const payload = await response.json() as { result?: T; error?: { code: string; message: string } }
      if (payload.error) throw new WorldError(payload.error.code, payload.error.message)
      return payload.result as T
    } catch (error) { status = 'error'; throw error }
    finally {
      this.trace({ kind: 'rpc', ...this.timingScope.getStore(), ...this.identity.getStore(), operation,
        started_at_ms, duration_ms: performance.now() - started, status,
        ...(operation === 'fs.probe' ? { path_count: (args as { paths: string[] }).paths.length } : {}) })
    }
  }
}

export default World
