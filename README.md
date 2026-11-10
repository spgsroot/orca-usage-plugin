# OMP Usage (Orca plugin)

A token-usage dashboard for the local [omp](https://omp.sh) CLI corpus — the plugin
form of the omp usage provider from
[stablyai/orca PR #36454](https://github.com/stablyai/orca/pull/36454).

![OMP Usage panel](docs/panel-dark.png)
![Status bar item](docs/statusbar-dark.png)

## What it does

- **Status bar item** — today's corpus token total and cost (e.g. `899.4M tok`), a
  tooltip with the exact numbers, refreshed on load and every 15 minutes;
- **OMP Usage panel** (right sidebar, `bar-chart-3` icon):
  - KPI strip: window tokens / window cost / today / daily average, each with its
    active-days context;
  - stacked daily token chart (pure CSS grid, no chart library) with a y-axis,
    gridlines, hover and keyboard-focusable tooltips per day, and pill-shaped
    series toggles — hide a series (say, dominant cache reads) and the chart
    rescales to what remains;
  - 7/14/30-day windows; per-model table (input / output / cache / reasoning /
    total / cost / sessions) over the whole corpus;
  - live scan progress badge while a rescan actually runs, with self-healing
    when the worker restarts.

## Where the data comes from

`~/.omp/agent/sessions/**/*.jsonl` — omp transcripts that already carry a
`usage` block per assistant message, with cost precomputed. The plugin keeps a
per-file size+mtime checkpoint cache under
`~/.omp/agent/orca-plugins/omp-usage/`, so unchanged sessions are never
re-parsed. Totals are corpus-wide (not per-worktree); reasoning tokens are kept
separate from output. The panel says so explicitly.

## Install

Requires Orca >= 1.4.255 (desktop app).

```sh
orca plugin dev load /path/to/orca-usage-plugin
# approval_required → re-run with --approve <fingerprint> from the output
```

Or list it in a catalog / install from a Git repository whose owner matches the
publisher you want. The plugin is fully self-contained: `node:*` imports only,
no build step, no other plugin, no network access.

## Capabilities

| Capability | Why |
| --- | --- |
| `storage:own` | Persist the scan checkpoint cache |
| `notifications:show` | (reserved) user-facing refresh notices |

The status bar item and panel run from a `trusted-worker`; the manifest is
[`orca-plugin.json`](orca-plugin.json).

## Verified

- Live in Orca 1.4.255: worker scans 590+ session files, 570+ sessions cached;
  both panels render with real data (`orca plugin shot`);
- interactive: series toggles rescale the chart (verified via DOM tests),
  tooltips on hover and focus, 7/14/30-day windows re-query;
- manual: Refresh rescans and updates the status bar; worker restart is
  retried with backoff by the panel.