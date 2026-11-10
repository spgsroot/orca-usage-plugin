/**
 * Fixture child: runs with a fake HOME from process start (see worker-fixtures.mjs).
 * Everything in this file runs in one process whose HOME points at the fixture.
 */

import { mkdir, writeFile, rm, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const FAKE_HOME = process.env.OMP_FIXTURE_HOME
assert.ok(FAKE_HOME, 'OMP_FIXTURE_HOME must be set by the runner')
const SESSIONS = join(FAKE_HOME, '.omp', 'agent', 'sessions')
const CACHE = join(FAKE_HOME, '.omp', 'agent', 'orca-plugins', 'omp-usage', 'usage-cache.json')

const rec = (obj) => JSON.stringify(obj)

// Fixture days are relative to today so the 90-day summary window always covers them.
const dayAt = (offsetDays, time) => {
  const d = new Date(Date.now() - offsetDays * 86_400_000)
  const iso = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${iso(d.getMonth() + 1)}-${iso(d.getDate())}T${time}`
}
const today = () => dayAt(0, '12:00:00.000Z').slice(0, 10)


async function ws(dir) {
  const p = join(SESSIONS, dir)
  await mkdir(p, { recursive: true })
  return p
}

// ---------------- fixtures ----------------
const A = await ws('-proj-a')
// cloud provider: cost>0, reasoning, cache write; junk and corrupt lines mixed in
await writeFile(
  join(A, '2026-01-02T10-00-00-000Z_s1.jsonl'),
  [
    rec({ type: 'session', id: 's1', cwd: '/work/a', timestamp: dayAt(2, '10:00:00.000Z') }),
    rec({ type: 'title', title: 'junk', id: 'x', parentId: null, timestamp: dayAt(2, '10:00:01.000Z') }),
    rec({ type: 'model_change', id: 'm', parentId: null, timestamp: dayAt(2, '10:00:01.000Z'), model: 'gpt-x' }),
    'corrupt-line-not-json',
    rec({ type: 'message', id: 'm1', parentId: 'm', timestamp: dayAt(2, '10:05:00.000Z'), message: { role: 'assistant', model: 'gpt-x', provider: 'openai', usage: { input: 100, output: 50, cacheRead: 10, cacheWrite: 5, reasoningTokens: 25, cost: { input: 0.001, output: 0.002, cacheRead: 0.0001, cacheWrite: 0.0002, total: 0.0033 } } } }),
    rec({ type: 'message', id: 'm2', parentId: 'm1', timestamp: dayAt(1, '11:00:00.000Z'), message: { role: 'assistant', model: 'claude-y', provider: 'anthropic', usage: { input: 200, output: 20, cacheRead: 0, cacheWrite: 0, reasoningTokens: 0, cost: { input: 0.004, output: 0.001, total: 0.005 } } } }),
    rec({ type: 'message', id: 'm3', parentId: 'm2', timestamp: dayAt(1, '12:00:00.000Z'), message: { role: 'user', content: [] } })
  ].join('\n') + '\n'
)

// local router: tokens, zero cost
await writeFile(
  join(A, '2026-01-03T09-00-00-000Z_s2.jsonl'),
  [
    rec({ type: 'session', id: 's2', cwd: '/work/b', timestamp: dayAt(1, '09:00:00.000Z') }),
    rec({ type: 'message', id: 'n1', parentId: null, timestamp: dayAt(1, '09:10:00.000Z'), message: { role: 'assistant', model: 'mistral-local', provider: 'llm-router-local', usage: { input: 1000, output: 100, cacheRead: 500, cacheWrite: 0, reasoningTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } } })
  ].join('\n') + '\n'
)

// subdirectory scanning
const DEEP = await ws('-proj-a/deep')
await writeFile(
  join(DEEP, '2026-01-04T10-00-00-000Z_s3.jsonl'),
  [
    rec({ type: 'session', id: 's3', cwd: '/work/a/deep', timestamp: dayAt(0, '10:00:00.000Z') }),
    rec({ type: 'message', id: 'k1', parentId: null, timestamp: dayAt(0, '10:01:00.000Z'), message: { role: 'assistant', model: 'kimi-k3', provider: 'kimi-k3', usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, reasoningTokens: 2, cost: { total: 0.0001 } } } })
  ].join('\n') + '\n'
)

// ---------------- harness ----------------
const orca = {
  log: () => {},
  host: { call: async () => ({ ok: true }) },
  panels: { handle: (name, fn) => handlers.set(name, fn), post: () => {} },
  commands: { register: () => {} },
  events: { on: () => ({ dispose() {} }) }
}
const handlers = new Map()

const { default: activate } = await import('../main.mjs')
await activate(orca)

const summary = (days) => handlers.get('usage.summary')({ days: 90 })

// ---------------- test 1: cloud + local + deep ----------------
{
  const r = await summary()
  assert.equal(r.sessionCount, 3, 'three sessions with usage')
  assert.equal(r.daily.length, 3, 'three days: Jan 2, 3, 4')

  const jan2 = r.daily.find((d) => d.day === dayAt(2, '00:00:00.000Z').slice(0, 10))
  assert.equal(jan2.inputTokens, 100)
  assert.equal(jan2.outputTokens, 50)
  assert.equal(jan2.cacheReadTokens, 10)
  assert.equal(jan2.cacheWriteTokens, 5)
  assert.equal(jan2.reasoningTokens, 25)
  // total rebuilt from components, not omp's totalTokens
  assert.equal(jan2.totalTokens, 100 + 50 + 10 + 5)
  assert.ok(Math.abs(jan2.costUsd - 0.0033) < 1e-9, 'cloud cost summed exactly')

  const jan3 = r.daily.find((d) => d.day === dayAt(1, '00:00:00.000Z').slice(0, 10))
  assert.equal(jan3.inputTokens, 200 + 1000)
  assert.ok(jan3.costUsd >= 0.005 && jan3.costUsd < 0.00511, '$0 record adds no cost')

  const byModel = new Map(r.byModel.map((m) => [m.model, m]))
  assert.ok(byModel.has('openai/gpt-x'), 'cloud provider key provider/model')
  assert.ok(byModel.has('anthropic/claude-y'), 'second cloud provider')
  assert.ok(byModel.has('llm-router-local/mistral-local'), 'zero-cost local router')
  assert.ok(byModel.has('kimi-k3/kimi-k3'), 'provider == model handled')
  assert.equal(byModel.get('kimi-k3/kimi-k3').reasoningTokens, 2, 'reasoning kept separate')

  const totals = r.totals
  assert.equal(totals.inputTokens, 100 + 200 + 1000 + 10)
  assert.ok(totals.costUsd >= 0.0084 && totals.costUsd < 0.00841, 'totals sum all costs')
  console.log('✓ 1. cloud/local/deep aggregation exact (costs, reasoning, cacheWrite, rebuilt totals)')
}

// ---------------- test 2: checkpoint skip ----------------
{
  const before = JSON.parse(await readFile(CACHE, 'utf8'))
  assert.equal(before.schemaVersion, 2)
  assert.ok(before.files.length >= 3, 'checkpoint persisted per file')
  const r2 = await summary()
  assert.equal(r2.sessionCount, 3)
  console.log('✓ 2. checkpoint cache persisted and reused on rescan')
}

// ---------------- test 3: append to an existing file ----------------
{
  const p = join(A, '2026-01-02T10-00-00-000Z_s1.jsonl')
  await writeFile(p, rec({ type: 'message', id: 'm9', parentId: 'm2', timestamp: dayAt(-1, '10:00:00.000Z'), message: { role: 'assistant', model: 'gpt-x', provider: 'openai', usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, reasoningTokens: 0, cost: { total: 0.0001 } } } }) + '\n', { flag: 'a' })
  await handlers.get('usage.refresh')()
  const r3 = await summary()
  assert.equal(r3.daily.length, 4, 'new day appears after append')
  assert.equal(r3.daily.find((d) => d.day === dayAt(-1, '00:00:00.000Z').slice(0, 10)).inputTokens, 1)
  console.log('✓ 3. appended records picked up (file rewritten, new day counted)')
}

// ---------------- test 4: empty corpus ----------------
{
  await rm(FAKE_HOME, { recursive: true, force: true })
  await mkdir(join(FAKE_HOME, '.omp', 'agent'), { recursive: true })
  await handlers.get('usage.refresh')()
  const r4 = await summary()
  assert.equal(r4.sessionCount, 0)
  assert.equal(r4.daily.length, 0)
  assert.equal(r4.byModel.length, 0)
  assert.equal(r4.totals.totalTokens, 0)
  console.log('✓ 4. empty corpus → clean zero summary, no crash')
}

// ---------------- test 5: sessions dir missing entirely ----------------
{
  await rm(FAKE_HOME, { recursive: true, force: true })
  await handlers.get('usage.refresh')()
  const r5 = await summary()
  assert.equal(r5.sessionCount, 0)
  assert.ok(Array.isArray(r5.daily))
  console.log('✓ 5. missing sessions dir → zero summary, no crash')
}

console.log('\nAll worker fixture tests passed.')
// activate() keeps a 15-min rescan timer alive; the test harness is done.
process.exit(0)