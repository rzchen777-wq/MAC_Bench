import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'
import { fixture } from './support.mts'
import { worldProviders } from '../../../src/mac_bench/baselines/superagent/world.ts'

const environment = await fixture({ workspace: process.env.MAC_TEST_WORKSPACE,
  world: worldProviders({ url: process.env.MAC_TEST_WORLD_URL!, tokenEnv: 'MAC_TEST_WORLD_KEY',
    workspace: process.env.MAC_TEST_WORKSPACE! }),
})
try {
  const controller = new AbortController()
  const handle = environment.context.subprocess.spawn({
    argv: ['/bin/bash', '-c', "python3 -c 'import sys,time; sys.stdout.write(\"x\"*2097152); sys.stdout.flush(); time.sleep(60)'"],
    cwd: environment.workspace, graceMs: 100, signal: controller.signal,
    stdio: { stdin: 'ignore', stdout: 'pipe', stderr: { maxBytes: 1024 } },
  })
  // Deliberately stop consuming the pipe, as a cancelled tool can do.
  await delay(500)
  controller.abort()
  await handle.waitForExit()
  assert.ok(['SIGTERM', 'SIGKILL'].includes((await handle.done).signal!))

  const echo = environment.context.subprocess.spawn({ argv: ['/bin/cat'],
    cwd: environment.workspace, graceMs: 100,
    stdio: { stdin: 'pipe', stdout: 'pipe', stderr: { maxBytes: 1024 } },
  })
  const chunks: Buffer[] = []
  echo.stdout!.on('data', chunk => chunks.push(chunk))
  echo.stdin!.end('shared process input\n')
  assert.equal((await echo.done).exitCode, 0)
  assert.equal(Buffer.concat(chunks).toString(), 'shared process input\n')

  const removed = environment.context.subprocess.spawn({
    argv: ['/bin/bash', '-c', 'printf "%s" "${MAC_TEST_AMBIENT-unset}"'],
    cwd: environment.workspace, graceMs: 100, env: { MAC_TEST_AMBIENT: undefined },
    stdio: { stdin: 'ignore', stdout: { maxBytes: 128 }, stderr: { maxBytes: 128 } },
  })
  assert.equal((await removed.done).exitCode, 0)
  assert.equal(removed.collected.stdout!.readFrom(0).text, 'unset', 'An explicit unset must survive JSON transport')
} finally {
  await environment.dispose()
}
