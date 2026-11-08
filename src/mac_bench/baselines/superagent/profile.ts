/** DSH services and role capabilities used by our superagent implementation. */
import { join } from 'node:path'
import type { EntryOptions } from '@deepseek-ai/cordis-plugin-loader'

export type Role = 'router' | 'worker' | 'subagent'
export const PRESETS: Record<Role, string> = {
  router: 'mac-superagent-router', worker: 'mac-superagent-worker', subagent: 'standard',
}

/** Providers execute inside the task world; the profile never chooses host IO. */
export interface WorldProviders {
  filesystem: EntryOptions
  subprocess: EntryOptions
  shell: (foregroundMs: number) => EntryOptions
  services?: EntryOptions[]
  tools?: EntryOptions[]
}

export interface ProfileOptions {
  stateDirectory: string
  workspace: string
  model: { provider: string; model: string }
  modelPlugins: EntryOptions[]
  world: WorldProviders
}

const plugin = (id: string, config?: unknown): EntryOptions => ({
  id, name: `@deepseek-ai/dsh-${id}`, ...(config === undefined ? {} : { config }),
})

// Presets mount below their own loader scope. Resolve package names here so
// their nested entries do not depend on where episode artifacts are stored.
function resolvePlugins(rows: EntryOptions[]): EntryOptions[] {
  return rows.map(row => ({ ...row,
    name: row.name?.startsWith('@') ? import.meta.resolve(row.name) : row.name,
    ...(Array.isArray(row.config) ? { config: resolvePlugins(row.config) }
      : row.config?.plugins ? { config: { ...row.config, plugins: resolvePlugins(row.config.plugins) } } : {}),
  }))
}

function rolePlugins(role: Role, world: WorldProviders, workspace: string): EntryOptions[] {
  // Upstream workers return from long commands after 5 s. Their spawned
  // children use standard's 60 s foreground window, but share the same world.
  const waitMs = role === 'subagent' ? 60_000 : 5_000
  const rows: EntryOptions[] = [
    ...(role === 'router' ? [] : [plugin('persona', {
      prefix: 'You are a coding agent powered by the {{model}} model.',
    })]),
    plugin('agent-instructions', { maxBytes: 65_536 }),
    plugin('time-context'),
    { id: 'shell-tools', name: 'cordis:group', group: true, isolate: { shell: true }, config: [
      world.shell(waitMs), plugin('tool-bash'),
    ] },
    plugin('tool-fs', role === 'router' ? { write: false, edit: false } : {}),
    ...(role === 'router' ? [] : [plugin('tool-fs-search', { sampleOverCapGlobResults: false })]),
    plugin('tool-jobs', { waitTimeoutMs: role === 'subagent' ? 30_000 : 5_000,
      maxWaitTimeoutMs: role === 'subagent' ? 30_000 : 5_000 }),
    plugin('skill-filesystem', { includeDefaultRoots: false, watch: false,
      customSkillDirs: [join(workspace, '.agents/skills'), join(workspace, '.dsh/skills')] }),
    plugin('tool-skill'),
    { id: 'context', name: 'cordis:group', group: true,
      isolate: { compaction: true, toolResultPruner: true }, config: [
        plugin('compaction-basic'), plugin('command-compact'),
        plugin('compaction-tool-result-pruner', { thresholdChars: 8192, headChars: 4096, tailChars: 1024 }),
      ] },
  ]
  if (role !== 'router') rows.push(
    plugin('tool-subagent-control'),
    { id: 'list-agents', name: '@deepseek-ai/dsh-tool-subagent-control/list-agents' },
    plugin('tool-subagent', { provider: 'spawn', toolName: 'subagent', modelSelectionSettings: true }),
    plugin('tool-todo', { allowParallelInProgress: true }),
    ...(world.tools ?? []),
  )
  return rows
}

/** Host state is episode-local; all role presets use the same execution backend. */
export function profile(options: ProfileOptions): EntryOptions[] {
  const state = options.stateDirectory
  return resolvePlugins([
    { id: 'timer', name: '@deepseek-ai/cordis-plugin-timer' },
    plugin('llm'), plugin('deepseek-llm-api-extensions'), plugin('llm-retry'),
    plugin('session'),
    plugin('session-title', { fallbackMaxWords: 5, fallbackMaxBytes: 40, maxTitleBytes: 80 }),
    plugin('session-projection'), plugin('session-projection-cache', { writeEveryEvents: 200, writeIntervalMs: 5000 }),
    plugin('session-persistence-jsonl', { root: join(state, 'sessions'), compression: 'none' }),
    // Native child listing/continuation queries the persisted session index.
    plugin('session-query-sqlite', { path: ':memory:', openAt: 'never' }),
    plugin('session-checkpoint-policy'),
    plugin('storage'), plugin('storage-json', { root: join(state, 'storage') }),
    plugin('storage-domain', { backend: 'json' }),
    plugin('attachment-local', { root: join(state, 'attachments') }),
    plugin('commands'),
    plugin('credentials-local', { path: join(state, 'credentials.yaml'), dshHome: state, watch: false }),
    plugin('agent'), plugin('agent-default-model', options.model),
    plugin('agent-loop', { agents: [] }),
    plugin('agent-preset-registry', { default: PRESETS.subagent }),
    plugin('working-directory', { defaultDirectory: options.workspace }),
    plugin('system-prompt', { personaPrefix: '' }), plugin('tools'), plugin('skill'),
    plugin('token-meter'), plugin('jobs-local'), plugin('subagent'),
    plugin('subagent-spawn-in-process', { providerName: 'spawn' }),
    { id: 'subagent-model-selection', name: '@deepseek-ai/dsh-tool-subagent/model-selection-settings',
      config: { enabled: true, allowedModels: [options.model] } },
    plugin('fs-observation-policy'),
    plugin('sandbox-policy', { mode: 'danger-full-access', workspaceRoot: options.workspace }),
    plugin('user-approval', { policy: 'never' }), plugin('permission-presets'),
    options.world.filesystem, options.world.subprocess, options.world.shell(60_000), ...(options.world.services ?? []),
    ...options.modelPlugins,
    ...(['router', 'worker', 'subagent'] as const).map(role => ({
      ...plugin('agent-preset', { id: PRESETS[role], name: role, plugins: rolePlugins(role, options.world, options.workspace) }),
      id: `preset-${role}`,
    })),
  ])
}
