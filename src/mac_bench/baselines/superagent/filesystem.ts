/** DSH file tools operating in the task sandbox, alongside its shell commands. */
import { posix } from 'node:path'
import { AsyncLocalStorage } from 'node:async_hooks'
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent'
import { FileSystem, FsError, FsTargetKey } from '@deepseek-ai/dsh-fs'
import type { FsDirEntry, FsEditOutcome, FsEditRequest, FsErrorCode, FsInfo,
  FsPathInfo, FsTarget, FsVersion, FsWriteIntent, FsWriteOutcome } from '@deepseek-ai/dsh-fs'
import { WorldError } from './world.ts'

interface Probe { path: string; targetKey: string; displayPath: string; info: FsInfo | null }
interface Discovery { active: boolean; paths: Map<string, Probe>; targets: Map<string, Probe> }
const candidates = ['.git', 'AGENTS.md', 'CLAUDE.md', 'AGENTS.local.md', 'CLAUDE.local.md']

export default class TaskFileSystem extends FileSystem {
  static inject = ['macWorld']
  private readonly world: Context['macWorld']
  private readonly discovery = new AsyncLocalStorage<Discovery>()

  constructor(ctx: Context) {
    super(ctx); this.world = ctx.macWorld
  }

  private snapshot(): Discovery | undefined {
    const value = this.discovery.getStore()
    return value?.active ? value : undefined
  }

  /** Called by the instruction plugin for both pre-step and file-touch refreshes. */
  withInstructionSnapshot<T>(details: { agent_id: string; refresh_phase: string },
    refresh: () => Promise<T>): Promise<T> {
    // A refresh can run after step/end as well as before the next model call.
    // Scope metadata to this refresh, not the whole model step or a time-to-live:
    // shell changes, other agents and later refreshes must see current files.
    const snapshot: Discovery = { active: true, paths: new Map(), targets: new Map() }
    return this.world.measure('instruction_refresh', details, () => this.discovery.run(snapshot, async () => {
      try { return await refresh() } finally { snapshot.active = false }
    }))
  }

  private async probe(path: string, cwd: string, signal?: AbortSignal): Promise<Probe | undefined> {
    const snapshot = this.snapshot()
    if (!snapshot) return undefined
    signal?.throwIfAborted()
    // Do not lexically collapse '..': a preceding component may be a symlink.
    const absolute = posix.isAbsolute(path) ? path : `${cwd}/${path}`
    if (snapshot.paths.has(absolute)) return snapshot.paths.get(absolute)
    const paths = new Set([absolute])
    for (const start of [cwd, posix.dirname(absolute)]) {
      for (let dir = start;; dir = posix.dirname(dir)) {
        paths.add(dir)
        for (const name of candidates) paths.add(`${dir === '/' ? '' : dir}/${name}`)
        if (posix.dirname(dir) === dir) break
      }
    }
    const missing = [...paths].filter(p => !snapshot.paths.has(p))
    for (let offset = 0; offset < missing.length; offset += 512) {
      const rows = await this.call<Probe[]>('probe', { paths: missing.slice(offset, offset + 512), cwd }, signal)
      for (const row of rows) {
        snapshot.paths.set(row.path, row)
        snapshot.targets.set(row.targetKey, row)
      }
    }
    return snapshot.paths.get(absolute)
  }

  private async call<T>(operation: string, args: unknown, signal?: AbortSignal): Promise<T> {
    try { return await this.world.request<T>(`fs.${operation}`, args, signal) }
    catch (error) {
      if (signal?.aborted) throw new FsError('File operation cancelled', 'FS_ABORTED', { cause: error })
      if (error instanceof WorldError && error.code.startsWith('FS_')) {
        throw new FsError(error.message, error.code as FsErrorCode, { cause: error })
      }
      throw error
    }
  }

  async resolve(path: string, opts?: { cwd?: string; signal?: AbortSignal }): Promise<FsTarget> {
    const value = await this.probe(path, opts?.cwd ?? this.world.workspace, opts?.signal)
      ?? await this.call<{ targetKey: string; displayPath: string }>('resolve',
      { path, cwd: opts?.cwd }, opts?.signal)
    return { displayPath: value.displayPath, targetKey: FsTargetKey(value.targetKey) }
  }
  processPath(target: FsTarget): string { return target.displayPath }
  fileUrl(target: FsTarget): string {
    return 'file://' + target.displayPath.split('/').map(encodeURIComponent).join('/')
  }
  contains(parent: FsTarget, child: FsTarget): boolean {
    const relative = posix.relative(parent.displayPath, child.displayPath)
    return relative === '' || (relative !== '..' && !relative.startsWith('../') && !posix.isAbsolute(relative))
  }
  async stat(target: FsTarget, signal?: AbortSignal): Promise<FsInfo | undefined> {
    signal?.throwIfAborted()
    const cached = this.snapshot()?.targets.get(target.targetKey)
    if (cached) return cached.info ?? undefined
    return (await this.call<FsInfo | null>('stat', { path: target.displayPath }, signal)) ?? undefined
  }
  async lstat(path: string, opts?: { cwd?: string }, signal?: AbortSignal): Promise<FsPathInfo | undefined> {
    return (await this.call<FsPathInfo | null>('lstat', { path, cwd: opts?.cwd }, signal)) ?? undefined
  }
  readText(target: FsTarget, signal?: AbortSignal): Promise<string> {
    return this.call('read_text', { path: target.displayPath }, signal)
  }
  async streamText(target: FsTarget, signal?: AbortSignal): Promise<AsyncIterable<string>> {
    const owner = this
    return (async function* () {
      const decoder = new TextDecoder('utf-8', { fatal: true })
      for (let offset = 0;; offset += 65536) {
        const bytes = await owner.readByteRange(target, { offset, length: 65536 }, signal)
        if (bytes.includes(0)) throw new FsError('The file contains binary data', 'FS_NOT_TEXT')
        let text: string
        try { text = decoder.decode(bytes, { stream: bytes.length === 65536 }) }
        catch (cause) { throw new FsError('The file is not UTF-8 text', 'FS_NOT_TEXT', { cause }) }
        if (text) yield text
        if (bytes.length < 65536) break
      }
    })()
  }
  async readBytes(target: FsTarget, signal: AbortSignal | undefined, maxBytes: number): Promise<Uint8Array> {
    return Buffer.from(await this.call<string>('read_bytes', { path: target.displayPath, maxBytes }, signal), 'base64')
  }
  async readByteRange(target: FsTarget, range: { offset: number; length: number }, signal?: AbortSignal): Promise<Uint8Array> {
    return Buffer.from(await this.call<string>('read_range', { path: target.displayPath, ...range }, signal), 'base64')
  }
  listDir(target: FsTarget, signal?: AbortSignal): Promise<FsDirEntry[]> {
    return this.call('list', { path: target.displayPath }, signal)
  }
  writeText(target: FsTarget, content: string, expected?: FsWriteIntent, signal?: AbortSignal): Promise<FsWriteOutcome> {
    return this.call('write', { path: target.displayPath, content, expected }, signal)
  }
  editText(target: FsTarget, edit: FsEditRequest, expected?: { version: FsVersion }, signal?: AbortSignal): Promise<FsEditOutcome> {
    return this.call('edit', { path: target.displayPath, edit, expected }, signal)
  }
}
