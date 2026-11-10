/**
 * Vitest wrapper around the fake-HOME fixture suite: spawns
 * worker-fixture-child.mjs with a fresh fake HOME (the worker resolves
 * ~/.omp at import time, so the child must start with HOME already set).
 */

import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))

describe('worker fixtures (fake HOME)', () => {
  it('cloud/local/deep aggregation, checkpoints, appends, empty states', async () => {
    const fakeHome = await mkdtemp(join(tmpdir(), 'omp-usage-fixture-'))
    try {
      const result = await new Promise((resolve) => {
        const child = spawn(
          process.execPath,
          [join(here, 'worker-fixture-child.mjs')],
          {
            env: { ...process.env, HOME: fakeHome, OMP_FIXTURE_HOME: fakeHome }
          }
        )
        let stdout = ''
        child.stdout.on('data', (chunk) => {
          stdout += String(chunk)
        })
        child.on('close', (code) => resolve({ code, stdout }))
      })
      expect(result.code, result.stdout).toBe(0)
    } finally {
      await rm(fakeHome, { recursive: true, force: true })
    }
  }, 120_000)
})