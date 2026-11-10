/**
 * Runner: spawns worker-fixture-child.mjs with a fake HOME set from process
 * start (the worker resolves ~/.omp from os.homedir(), which a mid-process
 * HOME change does not affect). Run: node test/worker-fixtures.mjs
 */

import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const fakeHome = await mkdtemp(join(tmpdir(), 'omp-usage-fixture-'))

try {
  const child = spawn(process.execPath, [join(here, 'worker-fixture-child.mjs')], {
    stdio: 'inherit',
    env: { ...process.env, HOME: fakeHome, OMP_FIXTURE_HOME: fakeHome }
  })
  const code = await new Promise((resolve) => child.on('close', resolve))
  if (code !== 0) {
    process.exitCode = code
  }
} finally {
  await rm(fakeHome, { recursive: true, force: true })
}