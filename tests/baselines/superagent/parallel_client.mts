import assert from 'node:assert/strict'
import { fixture, ScriptedModel, quiescent } from './support.mts'
import { Sessions } from '../../../src/mac_bench/baselines/superagent/sessions.ts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const environment = await fixture({ workspace: process.env.MAC_TEST_WORKSPACE,
  world: worldProviders({ url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY',
    workspace: process.env.MAC_TEST_WORKSPACE! }),
})
const { context, workspace } = environment
let dispatched = 0
const worked = new Set<string>()
const results: unknown[] = []
const errors: unknown[] = []
context.on('agent/error', ({ error }) => errors.push(error))
const adapter = new ScriptedModel(request => {
  if (request.sessionId === 'parallel-parent') {
    if (dispatched < 2) {
      const label = ['a', 'b'][dispatched++]
      return { name: 'subagent', arguments: {
        description: `Run parallel work ${label}`, prompt: `PARALLEL_${label}: run the shared workspace check.`,
      } }
    }
    return 'Both checks have been delegated.'
  }
  if (worked.has(request.sessionId)) {
    results.push({ agent: request.sessionId, messages: request.messages.slice(-2) })
    return 'The peer was present during execution.'
  }
  assert.equal(context.agentPresets.composedPreset(context.agents.get(request.sessionId).ctx), 'standard')
  worked.add(request.sessionId)
  const label = JSON.stringify(request.messages).includes('PARALLEL_a') ? 'a' : 'b'
  const peer = label === 'a' ? 'b' : 'a'
  // A serialized execution deadlocks until the deadline and fails: each shell
  // must observe the other live shell before either can finish.
  return { name: 'bash', arguments: { description: `Synchronize child ${label} with its peer`,
    command: `date +%s.%N; touch parallel_${label}.ready; deadline=$((SECONDS+45)); `
      + `while [ ! -f parallel_${peer}.ready ]; do `
      + 'if [ "$SECONDS" -ge "$deadline" ]; then exit 9; fi; sleep .05; done; '
      + `printf 'peer observed\\n' > parallel_${label}.done; date +%s.%N`,
  } }
})
context.llm.registerAdapter(['fixture'], adapter)
try {
  const sessions = new Sessions(context, workspace)
  const parent = await sessions.create('parallel-parent', 'worker', 'Delegate two independent checks.')
  await sessions.send(parent, 'task', 'Start both checks, then collect their results.')
  await quiescent(context, parent)
  assert.deepEqual(errors, [])
  const children = await context.subagents.listChildren(parent.id)
  assert.equal(children.length, 2)
  assert.equal(worked.size, 2)
  console.log(JSON.stringify({ results }))
  for (const label of ['a', 'b']) {
    assert.equal(await context.fs.readText(await context.fs.resolve(`parallel_${label}.done`)), 'peer observed\n')
  }
  console.log(JSON.stringify({ children: children.map(child => child.id), simultaneous_work: true }))
} finally {
  await environment.dispose()
}
