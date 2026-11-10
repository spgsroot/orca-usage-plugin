## Quality gates (eng-gates)

- After each small unit of work run `python3 tools/eng-gates/gate.py fast`. Before declaring a task done, or before
  review, run `python3 tools/eng-gates/gate.py full`. A task is not done while either fails.
- Read failures from the command output or `.gates/last.json`. Fix the cause in code or tests.
- Never edit gate configuration to make a gate pass: `gates.toml`, `tools/eng-gates/**`, `clippy.toml`, `deny.toml`,
  `ruff.toml`, `.importlinter`, `pyrightconfig.json`, `eslint.config.*`, `.dependency-cruiser.*`, `knip.json`,
  `tsconfig*.json`, `[lints]` in `Cargo.toml`, `[tool.*]` lint/test sections in `pyproject.toml`, CI workflows,
  benchmark baselines. If a rule looks wrong, stop and say so; a human decides. Never run `gate.py approve`.
- No new `#[allow]`, `# noqa`, `# type: ignore`, `eslint-disable`, `@ts-ignore`, `as any`, skipped or focused tests.
  If one is truly right, put `gate-allow: <reason>` on the same line; the reviewer reads every one.
- Never weaken a test to make it pass: no loosened assertions, no deleted cases, no mocked-away behaviour under test.
- Performance claims need a number from the benchmark gate, not a guess. Do not compare wall-clock times from
  this machine against CI; compare against the committed baseline.
