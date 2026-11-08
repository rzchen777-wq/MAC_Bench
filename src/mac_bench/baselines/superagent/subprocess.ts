/** Native DSH process handles backed by the task's persistent execution channel. */
import { randomUUID } from 'node:crypto'
import { PassThrough, Writable } from 'node:stream'
import type { Context } from '@deepseek-ai/cordis'
import { SubprocessRuntime, SubprocessExecutableNotFoundError } from '@deepseek-ai/dsh-subprocess'
import type { SubprocessHandle, SubprocessSpawnSpec, SubprocessOutcome,
  SubprocessOutputReader, SubprocessTerminalHandle, SubprocessTerminalSpawnSpec } from '@deepseek-ai/dsh-subprocess'
import { resolveRgPath } from '@deepseek-ai/dsh-tool-fs-search'
import { WorldError } from './world.ts'

interface WireOutput { data: string; nextOffset: number; endOffset: number; lossy: boolean; spillPath?: string }
interface Snapshot { done: boolean; exitCode: number | null; signal: NodeJS.Signals | null;
  stdout: WireOutput; stderr: WireOutput }

class Collected implements SubprocessOutputReader {
  private bytes = Buffer.alloc(0)
  offset = 0
  private spillPath?: string
  private readonly maximum: number
  constructor(maximum: number) { this.maximum = maximum }
  append(value: WireOutput): void {
    const bytes = Buffer.from(value.data, 'base64')
    this.bytes = value.lossy ? bytes : Buffer.concat([this.bytes, bytes])
    if (this.bytes.length > this.maximum) this.bytes = this.maximum ? this.bytes.subarray(-this.maximum) : Buffer.alloc(0)
    this.offset = value.nextOffset
    this.spillPath = value.spillPath
  }
  readFrom(fromByte: number) {
    const base = this.offset - this.bytes.length
    return { text: this.bytes.subarray(Math.max(0, fromByte - base)).toString('utf8'),
      nextOffset: this.offset, lossy: fromByte < base,
      ...(this.spillPath ? { spillPath: this.spillPath } : {}),
    }
  }
}

export default class TaskSubprocess extends SubprocessRuntime {
  static inject = ['macWorld']
  private readonly world: Context['macWorld']
  private readonly running = new Set<SubprocessHandle>()
  private readonly packagedRg = resolveRgPath()

  constructor(ctx: Context) {
    super(ctx)
    this.world = ctx.macWorld
    ctx.effect(() => async () => {
      const running = [...this.running]
      for (const process of running) process.terminate()
      await Promise.all(running.map(process => process.waitForExit()))
    })
  }

  async resolveExecutable(command: string, env?: Readonly<Record<string, string>>, signal?: AbortSignal): Promise<string> {
    try { return await this.world.request('fs.executable', { command, env }, signal) }
    catch (error) {
      if (error instanceof WorldError && error.code === 'FS_NOT_FOUND') {
        throw new SubprocessExecutableNotFoundError(error.message, { cause: error })
      }
      throw error
    }
  }
  async terminalEnvironment() { return { platform: 'posix' as const, defaultShell: '/bin/bash' } }
  async spawnTerminal(_spec: SubprocessTerminalSpawnSpec): Promise<SubprocessTerminalHandle> {
    throw new Error('MAC-Bench task tools use piped processes, not an interactive terminal')
  }

  spawn(spec: SubprocessSpawnSpec): SubprocessHandle {
    spec.signal?.throwIfAborted()
    const id = randomUUID()
    const world = this.world
    const collected = {
      ...(typeof spec.stdio.stdout === 'object' ? { stdout: new Collected(spec.stdio.stdout.maxBytes) } : {}),
      ...(typeof spec.stdio.stderr === 'object' ? { stderr: new Collected(spec.stdio.stderr.maxBytes) } : {}),
    }
    const stdout = spec.stdio.stdout === 'pipe' ? new PassThrough() : undefined
    const stderr = spec.stdio.stderr === 'pipe' ? new PassThrough() : undefined
    const started = (async () => {
      const argv = [...spec.argv]
      // Native glob/grep use DSH's packaged host rg path. Resolve that one
      // known executable in the task world; arbitrary user paths stay intact.
      if (argv[0] === await this.packagedRg) argv[0] = await this.resolveExecutable('rg')
      await world.request('process.spawn', { id, argv, cwd: spec.cwd, stdio: spec.stdio,
        // JSON omits undefined fields; preserve explicit environment removals.
        env: spec.env && Object.fromEntries(Object.entries(spec.env).map(([key, value]) => [key, value ?? null])),
        graceMs: spec.graceMs })
    })()
    const stdin = spec.stdio.stdin === 'pipe' ? new Writable({
      write(chunk, _encoding, callback) {
        started.then(() => world.request('process.write', { id, data: Buffer.from(chunk).toString('base64'), eof: false }))
          .then(() => callback(), callback)
      },
      final(callback) {
        started.then(() => world.request('process.write', { id, data: '', eof: true })).then(() => callback(), callback)
      },
    }) : undefined
    let stopped: Promise<unknown> | undefined
    let finished = false
    const terminate = () => {
      if (finished || stopped) return
      // A cancelled consumer may stop reading a pipe. Drain buffered output so
      // joining the terminated process cannot wait forever on backpressure.
      stdout?.resume(); stderr?.resume()
      stopped = started.then(() => world.request('process.terminate', { id }))
      stopped.catch(() => {}) // The joined handle reports transport failure.
    }
    const abort = () => terminate()
    spec.signal?.addEventListener('abort', abort, { once: true })
    if (spec.signal?.aborted) terminate()
    const done = (async (): Promise<SubprocessOutcome> => {
      try {
        await started
        let stdoutOffset = 0, stderrOffset = 0
        for (;;) {
          const snapshot = await world.request<Snapshot>('process.poll', { id, stdoutOffset, stderrOffset, waitMs: 30_000 })
          for (const stream of ['stdout', 'stderr'] as const) {
            const value = snapshot[stream]
            const sink = stream === 'stdout' ? stdout : stderr
            if (sink) {
              const bytes = Buffer.from(value.data, 'base64')
              if (bytes.length && !sink.write(bytes)) await new Promise<void>((resolve, reject) => {
                const cleanup = () => { sink.off('drain', drained); sink.off('close', drained); sink.off('error', failed) }
                const drained = () => { cleanup(); resolve() }
                const failed = (error: Error) => { cleanup(); reject(error) }
                sink.once('drain', drained); sink.once('close', drained); sink.once('error', failed)
              })
            } else if (spec.stdio[stream] === 'inherit') {
              process.stderr.write(Buffer.from(value.data, 'base64'))
            } else collected[stream]?.append(value)
          }
          stdoutOffset = snapshot.stdout.nextOffset; stderrOffset = snapshot.stderr.nextOffset
          if (snapshot.done && stdoutOffset >= snapshot.stdout.endOffset && stderrOffset >= snapshot.stderr.endOffset) {
            await stopped
            return { exitCode: snapshot.exitCode, signal: snapshot.signal }
          }
        }
      } finally {
        finished = true
        spec.signal?.removeEventListener('abort', abort)
        stdout?.end(); stderr?.end()
      }
    })()
    done.catch(() => {})
    const handle: SubprocessHandle = { stdin, stdout, stderr, control: undefined, collected, done, terminate,
      async waitForExit(signal) {
        if (signal?.aborted) return false
        if (!signal) { await done; return true }
        return new Promise<boolean>((resolve, reject) => {
          const aborted = () => { signal.removeEventListener('abort', aborted); resolve(false) }
          signal.addEventListener('abort', aborted, { once: true })
          done.then(() => { signal.removeEventListener('abort', aborted); resolve(true) },
            error => { signal.removeEventListener('abort', aborted); reject(error) })
          if (signal.aborted) aborted()
        })
      },
    }
    this.running.add(handle)
    done.then(() => this.running.delete(handle), () => this.running.delete(handle))
    return handle
  }
}
