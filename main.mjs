/**
 * OMP Usage worker — token-usage dashboard for the local omp corpus
 * (~/.omp/agent/sessions, all jsonl files), the plugin form of the omp usage provider
 * from stablyai/orca PR #36454: per-file size+mtime checkpoints, session totals
 * from `usage` records (cost precomputed by omp), reasoning tokens kept separate,
 * local-day buckets, live scan progress pushed to the panel.
 *
 * Self-contained: node:* only, no build step, no other plugin.
 */

import { readdir, readFile, writeFile, mkdir, stat } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import os from 'node:os'

const USAGE_DIR = join(os.homedir(), '.omp', 'agent', 'sessions')
const CACHE_PATH = join(
  os.homedir(),
  '.omp',
  'agent',
  'orca-plugins',
  'omp-usage',
  'usage-cache.json'
)
const CACHE_SCHEMA_VERSION = 2
const RESCAN_INTERVAL_MS = 15 * 60_000

// ---------------------------------------------------------------------------
// Transcript parsing
// ---------------------------------------------------------------------------

function positiveFinite(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0
}

function extractString(value) {
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** Local calendar day (YYYY-MM-DD) for an ISO timestamp; null when unparsable. */
function localDay(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate()
  ).padStart(2, '0')}`
}

function emptyTotals() {
  return {
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
    costUsd: 0
  }
}

function createAccumulator() {
  return {
    firstUsageAt: null,
    lastUsageAt: null,
    lastUsageModel: null,
    lastUsageProvider: null,
    totals: emptyTotals(),
    dailyByDay: new Map(),
    /** Exact per-model totals: every usage block is attributed to the model of its own message. */
    byModel: new Map()
  }
}

/** Fold one transcript line; session/model context is tracked, non-usage records ignored. */
function applyLine(line, context, accumulator) {
  let parsed
  try {
    parsed = JSON.parse(line)
  } catch {
    return
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return
  const record = parsed
  const type = extractString(record.type)

  if (type === 'session') {
    const id = extractString(record.id)
    if (id) context.sessionId = id
    context.cwd = extractString(record.cwd) ?? context.cwd
    context.sessionStartedAt = extractString(record.timestamp) ?? context.sessionStartedAt
    return
  }
  if (type === 'model_change') {
    context.currentModel = extractString(record.model) ?? context.currentModel
    return
  }
  if (type !== 'message') return

  const message = record.message
  if (message === null || typeof message !== 'object' || Array.isArray(message)) return
  context.currentModel = extractString(message.model) ?? context.currentModel
  context.currentProvider = extractString(message.provider) ?? context.currentProvider

  const usage = message.usage
  if (usage === null || typeof usage !== 'object' || Array.isArray(usage)) return

  const inputTokens = positiveFinite(usage.input)
  const outputTokens = positiveFinite(usage.output)
  const cacheReadTokens = positiveFinite(usage.cacheRead)
  const cacheWriteTokens = positiveFinite(usage.cacheWrite)
  const reasoningTokens = positiveFinite(usage.reasoningTokens)
  if (inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens + reasoningTokens === 0) {
    return
  }

  const timestamp = extractString(record.timestamp)
  const day = timestamp ? localDay(timestamp) : null
  if (!timestamp || !day) return
  const cost =
    usage.cost !== null && typeof usage.cost === 'object' && !Array.isArray(usage.cost)
      ? positiveFinite(usage.cost.total)
      : 0
  // Rebuild the total from components: omp's own totalTokens may exclude reasoning.
  const totalTokens = inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens

  const t = accumulator.totals
  t.inputTokens += inputTokens
  t.outputTokens += outputTokens
  t.cacheReadTokens += cacheReadTokens
  t.cacheWriteTokens += cacheWriteTokens
  t.reasoningTokens += reasoningTokens
  t.totalTokens += totalTokens
  t.costUsd += cost

  if (!accumulator.firstUsageAt || timestamp < accumulator.firstUsageAt) {
    accumulator.firstUsageAt = timestamp
  }
  if (!accumulator.lastUsageAt || timestamp >= accumulator.lastUsageAt) {
    accumulator.lastUsageAt = timestamp
    accumulator.lastUsageModel = context.currentModel ?? 'unknown'
    accumulator.lastUsageProvider = context.currentProvider ?? 'unknown'
  }

  // Per-model attribution uses the message's own model, so a session that
  // switches models keeps each model's cost exact.
  const modelKey =
    context.currentProvider !== null
      ? `${context.currentProvider}/${context.currentModel ?? 'unknown'}`
      : (context.currentModel ?? 'unknown')
  let modelBucket = accumulator.byModel.get(modelKey)
  if (!modelBucket) {
    modelBucket = { model: modelKey, ...emptyTotals(), sessions: 0 }
    accumulator.byModel.set(modelKey, modelBucket)
  }
  modelBucket.inputTokens += inputTokens
  modelBucket.outputTokens += outputTokens
  modelBucket.cacheReadTokens += cacheReadTokens
  modelBucket.cacheWriteTokens += cacheWriteTokens
  modelBucket.reasoningTokens += reasoningTokens
  modelBucket.totalTokens += totalTokens
  modelBucket.costUsd += cost

  let daily = accumulator.dailyByDay.get(day)
  if (!daily) {
    daily = { day, ...emptyTotals() }
    accumulator.dailyByDay.set(day, daily)
  }
  daily.inputTokens += inputTokens
  daily.outputTokens += outputTokens
  daily.cacheReadTokens += cacheReadTokens
  daily.cacheWriteTokens += cacheWriteTokens
  daily.reasoningTokens += reasoningTokens
  daily.totalTokens += totalTokens
  daily.costUsd += cost
}

function finalizeSession(context, accumulator) {
  if (!accumulator.lastUsageAt) return null
  return {
    sessionId: context.sessionId ?? 'unknown',
    startedAt: context.sessionStartedAt ?? accumulator.firstUsageAt,
    endedAt: accumulator.lastUsageAt,
    cwd: context.cwd ?? '',
    model: accumulator.lastUsageModel ?? 'unknown',
    provider: accumulator.lastUsageProvider ?? 'unknown',
    ...accumulator.totals
  }
}

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------

async function listSessionFiles(dir) {
  const out = []
  const stack = [dir]
  while (stack.length > 0) {
    const current = stack.pop()
    let entries
    try {
      entries = await readdir(current, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      const full = join(current, entry.name)
      if (entry.isDirectory()) stack.push(full)
      else if (entry.isFile() && entry.name.endsWith('.jsonl')) out.push(full)
    }
  }
  out.sort()
  return out
}

function addTotals(target, delta) {
  target.inputTokens += delta.inputTokens
  target.outputTokens += delta.outputTokens
  target.cacheReadTokens += delta.cacheReadTokens
  target.cacheWriteTokens += delta.cacheWriteTokens
  target.reasoningTokens += delta.reasoningTokens
  target.totalTokens += delta.totalTokens
  target.costUsd += delta.costUsd
}

async function scanFile(filePath, previous) {
  const info = await stat(filePath)
  // Append-only files: unchanged size+mtime means the parsed projection is still valid.
  if (previous && previous.size === info.size && previous.mtimeMs === info.mtimeMs) {
    return previous
  }
  let content
  try {
    content = await readFile(filePath, 'utf8')
  } catch {
    return previous ?? null
  }
  const context = {
    sessionId: null,
    sessionStartedAt: null,
    cwd: null,
    currentModel: null,
    currentProvider: null
  }
  const accumulator = createAccumulator()
  for (const line of content.split('\n')) {
    if (line.length > 0) applyLine(line, context, accumulator)
  }
  const session = finalizeSession(context, accumulator)
  return {
    path: filePath,
    mtimeMs: info.mtimeMs,
    size: info.size,
    sessions: session ? [session] : [],
    dailyAggregates: [...accumulator.dailyByDay.values()],
    byModelBuckets: [...accumulator.byModel.values()]
  }
}

function summarize(files) {
  const byDay = new Map()
  for (const file of files) {
    for (const daily of file.dailyAggregates) {
      let entry = byDay.get(daily.day)
      if (!entry) {
        byDay.set(daily.day, { ...daily })
        continue
      }
      addTotals(entry, daily)
    }
  }
  const daily = [...byDay.values()].sort((a, b) => (a.day < b.day ? -1 : 1))

  const sessions = files.flatMap((f) => f.sessions)
  // Per-model buckets are exact: every usage block is attributed to the model of
  // its own message. A model's session count is how many sessions used it.
  const byModel = new Map()
  for (const file of files) {
    const counted = new Set()
    for (const bucket of file.byModelBuckets ?? []) {
      let entry = byModel.get(bucket.model)
      if (!entry) {
        entry = { ...bucket, sessions: 0 }
        byModel.set(bucket.model, entry)
      } else {
        addTotals(entry, bucket)
      }
      if (!counted.has(bucket.model)) {
        counted.add(bucket.model)
        entry.sessions += 1
      }
    }
  }

  const totals = emptyTotals()
  for (const d of daily) addTotals(totals, d)
  return { sessions, daily, byModel: [...byModel.values()], totals }
}

// ---------------------------------------------------------------------------
// Persisted checkpoint + progress
// ---------------------------------------------------------------------------

const state = { cache: null }
const scanState = { progress: null }
let scanInFlight = null
let activeOrca = null

async function loadCache() {
  if (state.cache) return state.cache
  try {
    const raw = JSON.parse(await readFile(CACHE_PATH, 'utf8'))
    if (raw.schemaVersion === CACHE_SCHEMA_VERSION && Array.isArray(raw.files)) {
      state.cache = raw
      return raw
    }
  } catch {}
  state.cache = { schemaVersion: CACHE_SCHEMA_VERSION, files: [] }
  return state.cache
}

async function saveCache(cache) {
  state.cache = cache
  try {
    await mkdir(dirname(CACHE_PATH), { recursive: true })
    await writeFile(CACHE_PATH, JSON.stringify(cache), 'utf8')
  } catch {
    // a read-only HOME degrades to in-memory scanning; correctness is unaffected
  }
}

/** Best-effort progress push to open panels; no listener means no-op. */
function postToPanels(event, payload) {
  try {
    activeOrca?.panels?.post?.(event, payload)
  } catch {
    // panels.post is optional while the worker has no panel bound yet
  }
}

function ensureScan(force = false) {
  if (force) state.cache = null
  if (!scanInFlight) {
    scanInFlight = (async () => {
      const cache = await loadCache()
      const previousByPath = new Map(cache.files.map((f) => [f.path, f]))
      const paths = await listSessionFiles(USAGE_DIR)
      const files = []
      const startedAt = Date.now()
      let index = 0
      let reportedAt = 0
      let progressShown = false
      scanState.progress = { scanned: 0, total: paths.length }
      for (const filePath of paths) {
        try {
          const outcome = await scanFile(filePath, previousByPath.get(filePath))
          if (outcome) files.push(outcome)
        } catch {
          // unreadable or vanished files are skipped, as in the PR
        }
        index += 1
        const now = Date.now()
        // Cached passes finish in well under a second; showing a progress badge for
        // them makes every panel open flash "scanning" for no reason. Report progress
        // only once the scan has actually been running for a while.
        if (!progressShown && now - startedAt > 1000) {
          progressShown = true
          reportedAt = now
          postToPanels('usage.progress', { scanned: index, total: paths.length })
        } else if (progressShown && now - reportedAt >= 200) {
          reportedAt = now
          scanState.progress = { scanned: index, total: paths.length }
          postToPanels('usage.progress', scanState.progress)
        }
        // Yield regularly so a large corpus never blocks the worker's message loop.
        if (index % 10 === 0) await new Promise((resolve) => setTimeout(resolve, 0))
      }
      const summary = summarize(files)
      await saveCache({ schemaVersion: CACHE_SCHEMA_VERSION, files })
      const final = { scanned: paths.length, total: paths.length }
      scanState.progress = null
      postToPanels('usage.progress', final)
      return summary
    })().finally(() => {
      scanInFlight = null
    })
  }
  return scanInFlight
}

function tokenCount(n) {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

export default async function activate(orca) {
  activeOrca = orca
  let statusTimer = null

  async function updateStatusBar() {
    try {
      const summary = await ensureScan()
      const today = localDay(new Date().toISOString())
      const todayTotal = summary.daily.find((d) => d.day === today)
      const tokens = todayTotal ? todayTotal.totalTokens : 0
      const cost = todayTotal ? todayTotal.costUsd : 0
      await orca.host.call('statusBar.update', {
        id: 'omp-tokens',
        text: `${tokenCount(tokens)} tok`,
        tone: 'neutral',
        tooltip: `OMP today: ${tokens.toLocaleString()} tokens · $${cost.toFixed(4)} (corpus-wide, all local sessions)`
      })
    } catch (error) {
      orca.log(`[omp-usage] status bar update failed: ${String(error?.message ?? error)}`)
    }
  }

  orca.panels.handle('usage.summary', async (input) => {
    const days =
      typeof input?.days === 'number' && Number.isFinite(input.days)
        ? Math.max(1, Math.min(90, Math.floor(input.days)))
        : 30
    const summary = await ensureScan()
    const cutoffDay = localDay(new Date(Date.now() - (days - 1) * 86_400_000).toISOString())
    const daily = summary.daily.filter((d) => d.day >= cutoffDay)
    return {
      scannedAt: Date.now(),
      days,
      daily,
      byModel: summary.byModel,
      totals: summary.totals,
      sessionCount: summary.sessions.length,
      scanning: scanState.progress
    }
  })

  orca.panels.handle('usage.refresh', async () => {
    await ensureScan(true)
    await updateStatusBar()
    return { ok: true }
  })

  orca.commands.register('omp-usage.refresh', async () => {
    await ensureScan(true)
    await updateStatusBar()
    const summary = await ensureScan()
    const last = summary.daily.at(-1)
    return last
      ? `Usage refreshed. Latest day ${last.day}: ${last.totalTokens.toLocaleString()} tokens, $${last.costUsd.toFixed(4)}.`
      : 'Usage refreshed. No omp session data found.'
  })

  // The status bar item keeps this worker alive while the plugin is enabled.
  await updateStatusBar()
  statusTimer = setInterval(() => {
    void ensureScan(true).then(() => updateStatusBar())
  }, RESCAN_INTERVAL_MS)

  return () => {
    clearInterval(statusTimer)
  }
}