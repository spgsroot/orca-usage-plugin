#!/usr/bin/env python3
"""eng-gates: deterministic quality gates for Rust, Python and JS/TS projects.

Usage:
  gate.py fast   [--all] [--json] [--base REF]   # seconds: format, lint, types, tests, boundaries, guards
  gate.py full   [--all] [--json] [--base REF]   # fast + deps, CRAP, duplication, mutation, benchmarks
  gate.py doctor                                 # what is detected, what each check would run, what is missing
  gate.py init   [--force]                       # copy config templates and vendor the runner into the repo
  gate.py approve                                # HUMAN ONLY: accept the current state of protected config

Exit codes: 0 = no failures, 1 = at least one check failed, 2 = usage or configuration error.
Only the Python standard library is used (3.11+).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_NAME = "gates.toml"
STATE_DIR = ".gates"
VENDOR_DIR = "tools/eng-gates"
TAIL_LINES = 40

LANG_EXT = {
    "rust": {".rs"},
    "python": {".py", ".pyi"},
    "ts": {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"},
}
LANG_MARKERS = {
    "rust": ["Cargo.toml"],
    "python": ["pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"],
    "ts": ["package.json"],
}
# Files whose change re-triggers a language even if no source file changed.
LANG_CONFIG_FILES = {
    "rust": {"Cargo.toml", "Cargo.lock", "clippy.toml", ".clippy.toml", "deny.toml", "rustfmt.toml", "rust-toolchain.toml"},
    "python": {"pyproject.toml", "uv.lock", "requirements.txt", "ruff.toml", ".importlinter", "pyrightconfig.json", "mypy.ini", "tach.toml"},
    "ts": {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "eslint.config.mjs", ".dependency-cruiser.cjs", "knip.json"},
}

# Protected whole files: the agent must not weaken the gates by editing them.
PROTECTED_FILES = [
    "gates.toml", f"{VENDOR_DIR}/**",
    "clippy.toml", ".clippy.toml", "deny.toml", "rustfmt.toml", ".rustfmt.toml", "rust-toolchain.toml",
    "ruff.toml", ".ruff.toml", ".importlinter", "tach.toml", "pyrightconfig.json", "mypy.ini", ".coveragerc",
    "eslint.config.*", ".eslintrc*", ".dependency-cruiser.*", "knip.json", "knip.jsonc", ".size-limit.*",
    "tsconfig*.json", "vitest.config.*", "jest.config.*",
    "benches/baselines/**", ".gates/baselines/**", ".metrics/baselines/**",
    ".github/workflows/**", ".gitlab-ci.yml", ".pre-commit-config.yaml",
]
# Protected sections inside files that also hold ordinary content.
PROTECTED_TOML_SECTIONS = {
    "Cargo.toml": [("lints",), ("workspace", "lints"), ("profile",)],
    "pyproject.toml": [("tool", k) for k in (
        "ruff", "mypy", "pyright", "importlinter", "pytest", "coverage", "deptry", "tach")],
}
PROTECTED_JSON_KEYS = {"package.json": [("eslintConfig",), ("size-limit",), ("knip",),
                                        ("scripts", "test"), ("scripts", "lint"), ("scripts", "typecheck")]}

# New suppressions in the diff. A reason after "gate-allow:" downgrades the hit to a warning.
SUPPRESSIONS = {
    "rust": [r"#!?\[\s*allow\s*\(", r"#!?\[\s*expect\s*\(", r"#\[\s*ignore\s*\]", r"#\[\s*cfg_attr\s*\([^)]*allow"],
    "python": [r"#\s*noqa", r"#\s*type:\s*ignore", r"#\s*pyright:\s*ignore", r"#\s*pragma:\s*no\s*cover",
               r"@pytest\.mark\.(skip|xfail)", r"\bpytest\.skip\(", r"#\s*ruff:\s*noqa"],
    "ts": [r"eslint-disable", r"@ts-ignore", r"@ts-expect-error", r"@ts-nocheck", r"\b(it|test|describe)\.skip\(",
           r"(istanbul|c8|v8)\s+ignore", r"\bas\s+any\b"],
}
NEVER_ALLOWED = [r"\b(it|test|describe)\.only\(", r"\bfdescribe\(", r"\bfit\("]
ALLOW_RE = re.compile(r"gate-allow:\s*\S")


@dataclass
class Check:
    id: str
    lang: str            # rust | python | ts | any
    tier: str            # fast | full
    desc: str
    variants: list       # [(tool, argv-template)], first one whose tool resolves wins
    requires: list = field(default_factory=list)   # files that must exist (globs, any match)
    enabled: bool = True
    parse: str | None = None    # name of an output parser that can turn exit 0 into a failure
    internal: str | None = None # name of a built-in check instead of a command


def builtin_checks() -> list[Check]:
    c = []
    # ---------- Rust ----------
    c.append(Check("rust.fmt", "rust", "fast", "rustfmt", [("cargo", "cargo fmt --all --check")]))
    c.append(Check("rust.clippy", "rust", "fast", "clippy, warnings are errors",
                   [("cargo", "cargo clippy --workspace --all-targets --all-features -- -D warnings")]))
    c.append(Check("rust.test", "rust", "fast", "tests",
                   [("cargo-nextest", "cargo nextest run --workspace --no-tests=pass"), ("cargo", "cargo test --workspace")]))
    c.append(Check("rust.deny", "rust", "full", "advisories, licenses, banned crates, sources",
                   [("cargo-deny", "cargo deny check")], requires=["deny.toml"]))
    c.append(Check("rust.machete", "rust", "full", "unused dependencies", [("cargo-machete", "cargo machete")]))
    c.append(Check("rust.semver", "rust", "full", "public API semver breakage (published libraries)",
                   [("cargo-semver-checks", "cargo semver-checks")], enabled=False))
    c.append(Check("rust.bench", "rust", "full", "benchmarks with regression limits (gungraun / codspeed)",
                   [("cargo", "cargo bench")], enabled=False))
    # ---------- Python ----------
    c.append(Check("py.format", "python", "fast", "ruff format", [("ruff", "ruff format --check {files:python}")]))
    c.append(Check("py.lint", "python", "fast", "ruff lint", [("ruff", "ruff check {files:python}")]))
    c.append(Check("py.types", "python", "fast", "static types",
                   [("pyright", "pyright"), ("mypy", "mypy .")],
                   requires=["pyrightconfig.json", "mypy.ini", "pyproject.toml"]))
    c.append(Check("py.test", "python", "fast", "pytest", [("pytest", "pytest -q -x")]))
    c.append(Check("py.imports", "python", "fast", "module boundaries",
                   [("lint-imports", "lint-imports"), ("tach", "tach check")],
                   requires=[".importlinter", "tach.toml"]))
    c.append(Check("py.deps", "python", "full", "missing / unused / transitive dependencies", [("deptry", "deptry .")]))
    c.append(Check("py.bench", "python", "full", "benchmarks against saved baseline",
                   [("pytest", "pytest benches --benchmark-only --benchmark-compare --benchmark-compare-fail=mean:5%")],
                   enabled=False))
    # ---------- JS / TS ----------
    c.append(Check("ts.types", "ts", "fast", "tsc --noEmit", [("tsc", "tsc --noEmit -p .")], requires=["tsconfig.json"]))
    c.append(Check("ts.lint", "ts", "fast", "eslint, warnings are errors",
                   [("eslint", "eslint --max-warnings 0 {files:ts}")],
                   requires=["eslint.config.*", ".eslintrc*"]))
    c.append(Check("ts.test", "ts", "fast", "tests", [("vitest", "vitest run"), ("jest", "jest --ci")]))
    c.append(Check("ts.deps", "ts", "fast", "module boundaries and cycles",
                   [("depcruise", "depcruise {src} --config .dependency-cruiser.cjs")],
                   requires=[".dependency-cruiser.cjs"]))
    c.append(Check("ts.knip", "ts", "full", "dead files, exports and dependencies", [("knip", "knip")]))
    c.append(Check("ts.size", "ts", "full", "bundle size budget", [("size-limit", "size-limit")],
                   requires=[".size-limit.json", ".size-limit.js", ".size-limit.cjs"]))
    c.append(Check("ts.bench", "ts", "full", "benchmarks against saved baseline", [("node", "node benches/run.mjs")],
                   enabled=False))
    # ---------- Uncle Bob's gates (ub-mode) ----------
    c.append(Check("ub.crap", "any", "full", "CRAP per function under threshold",
                   [("crapper", "crapper {changed_flag} --threshold {crap}")]))
    c.append(Check("ub.dry", "any", "full", "structural duplication",
                   [("dryer", "dryer {changed_flag} --threshold {dry}")], parse="dryer"))
    c.append(Check("ub.mutate", "any", "full", "mutation testing: no surviving mutants",
                   [("mutator", "mutator {changed_flag}")]))
    # ---------- guards ----------
    c.append(Check("guard.config", "any", "fast", "protected gate config unchanged", [], internal="config"))
    c.append(Check("guard.suppress", "any", "fast", "no new unexplained suppressions", [], internal="suppress"))
    return c


# ---------------------------------------------------------------- helpers

def sh(argv: list[str] | str, cwd: Path, timeout: int | None = None, shell: bool = False):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=shell,
                           errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[eng-gates] timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def git(root: Path, *args: str) -> tuple[int, str]:
    return sh(["git", *args], root)


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        if (d / CONFIG_NAME).exists() or (d / ".git").exists():
            return d
    return cur


def load_config(root: Path) -> dict:
    p = root / CONFIG_NAME
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        print(f"[eng-gates] {CONFIG_NAME}: {e}", file=sys.stderr)
        sys.exit(2)


def any_exists(root: Path, globs: list[str]) -> bool:
    return any(next(root.glob(g), None) is not None for g in globs)


def detect_languages(root: Path, cfg: dict) -> list[str]:
    forced = cfg.get("gate", {}).get("languages")
    if forced:
        return list(forced)
    return [lang for lang, markers in LANG_MARKERS.items() if any((root / m).exists() for m in markers)]


def is_git(root: Path) -> bool:
    return git(root, "rev-parse", "--is-inside-work-tree")[0] == 0


def resolve_base(root: Path, cfg: dict, override: str | None) -> str | None:
    if not is_git(root):
        return None
    candidates = [override] if override else [cfg.get("gate", {}).get("base"), "origin/main", "origin/master", "main", "master"]
    for ref in filter(None, candidates):
        code, out = git(root, "merge-base", ref, "HEAD")
        if code == 0:
            return out.strip()
    if git(root, "rev-parse", "--verify", "-q", "HEAD")[0] == 0:
        return "HEAD"
    return None  # repository without commits


def changed_files(root: Path, base: str | None) -> list[str] | None:
    """Files changed against base, including staged, unstaged and untracked. None = not a git repo."""
    if not is_git(root):
        return None
    files: set[str] = set()
    if base:
        code, out = git(root, "diff", "--name-only", "--diff-filter=ACMRT", base)
        if code == 0:
            files.update(line for line in out.splitlines() if line)
    else:  # no commits yet: everything staged counts
        files.update(line for line in git(root, "diff", "--cached", "--name-only")[1].splitlines() if line)
    files.update(line for line in git(root, "ls-files", "--others", "--exclude-standard")[1].splitlines() if line)
    return sorted(f for f in files if (root / f).exists())


def langs_touched(files: list[str]) -> set[str]:
    out = set()
    for f in files:
        name, ext = Path(f).name, Path(f).suffix
        for lang, exts in LANG_EXT.items():
            if ext in exts or name in LANG_CONFIG_FILES[lang]:
                out.add(lang)
    return out


def resolve_tool(root: Path, tool: str, cfg: dict) -> str | None:
    tools_cfg = cfg.get("tools", {})
    if tool in tools_cfg:
        p = Path(os.path.expanduser(tools_cfg[tool]))
        p = p if p.is_absolute() else root / p
        return str(p) if p.exists() else None
    if tool in ("crapper", "dryer", "mutator"):
        ub_dir = Path(os.path.expanduser(tools_cfg.get("ub_dir", os.environ.get(
            "ENG_GATES_UB_DIR", "~/.local/share/eng-gates/ub"))))
        p = ub_dir / tool / tool
        if p.exists():
            return str(p)
        return shutil.which(tool)
    if tool.startswith("cargo-"):
        return shutil.which(tool)
    for local in (root / "node_modules" / ".bin" / tool, root / ".venv" / "bin" / tool):
        if local.exists():
            return str(local)
    return shutil.which(tool)


def render(template: str, ctx: dict) -> list[str] | None:
    """Fill placeholders. Returns None when a {files:lang} placeholder has nothing to check."""
    argv = []
    scalars = {k: str(v) for k, v in ctx.items() if isinstance(v, (str, int, float))}
    for tok in shlex.split(template):
        m = re.fullmatch(r"\{files:(\w+)\}", tok)
        if m:
            files = ctx["files"].get(m.group(1))
            if files is None:
                argv.append(".")
            elif not files:
                return None
            else:
                argv.extend(files)
            continue
        val = re.sub(r"\{(\w+)\}", lambda mm: scalars.get(mm.group(1), mm.group(0)), tok)
        if val or not tok.startswith("{"):  # an empty placeholder ({changed_flag} in --all) disappears
            argv.append(val)
    return argv


def tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


# ---------------------------------------------------------------- guards

def table_at(data: dict, path: tuple):
    cur = data
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def file_at_base(root: Path, base: str | None, rel: str) -> str | None:
    if not base:
        return None
    code, out = git(root, "show", f"{base}:{rel}")
    return out if code == 0 else None


def protected_state(root: Path, cfg: dict) -> dict[str, str]:
    """Hash of every protected file / section as it is now (used by `approve`)."""
    state = {}
    globs = PROTECTED_FILES + cfg.get("gate", {}).get("protected", [])
    for g in globs:
        for p in root.glob(g):
            if p.is_file() and ".git" not in p.parts:
                state[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    for rel, sections in PROTECTED_TOML_SECTIONS.items():
        p = root / rel
        if p.exists():
            try:
                data = tomllib.loads(p.read_text())
            except tomllib.TOMLDecodeError:
                continue
            for s in sections:
                state[f"{rel}#{'.'.join(s)}"] = hashlib.sha256(
                    json.dumps(table_at(data, s), sort_keys=True, default=str).encode()).hexdigest()
    for rel, keys in PROTECTED_JSON_KEYS.items():
        p = root / rel
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            for k in keys:
                state[f"{rel}#{'.'.join(k)}"] = hashlib.sha256(
                    json.dumps(table_at(data, k), sort_keys=True).encode()).hexdigest()
    return state


def guard_config(root: Path, cfg: dict, base: str | None, changed: list[str] | None) -> tuple[str, str]:
    if changed is None:
        return "skip", "not a git repository: nothing to compare against"
    globs = PROTECTED_FILES + cfg.get("gate", {}).get("protected", [])
    hits: list[str] = [f for f in changed if any(fnmatch.fnmatch(f, g) for g in globs)]  # keys as in protected_state
    for rel, sections, loads, err in (
            *[(r, s, tomllib.loads, tomllib.TOMLDecodeError) for r, s in PROTECTED_TOML_SECTIONS.items()],
            *[(r, k, json.loads, json.JSONDecodeError) for r, k in PROTECTED_JSON_KEYS.items()]):
        if rel not in changed:
            continue
        try:
            new = loads((root / rel).read_text())
            old_text = file_at_base(root, base, rel)
            old = loads(old_text) if old_text else {}
        except err as e:
            hits.append(f"{rel}#(unparseable: {e})")
            continue
        hits += [f"{rel}#{'.'.join(s)}" for s in sections if table_at(new, s) != table_at(old, s)]
    if not hits:
        return "pass", ""
    listing = "\n  ".join(hits)
    approved = load_approval(root)
    if approved is not None and not os.environ.get("CI"):
        current = protected_state(root, cfg)
        if all(k in approved and current.get(k) == approved[k] for k in hits):
            return "pass", f"protected config changed, approved by a human (gate.py approve):\n  {listing}"
    return "fail", ("protected gate configuration changed. Agents must not edit these: revert the change, "
                    f"or stop and ask a human to review it (they run `gate.py approve`).\n  {listing}")


def approval_path(root: Path) -> Path:
    return root / STATE_DIR / "approved.json"


def load_approval(root: Path) -> dict | None:
    p = approval_path(root)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except json.JSONDecodeError:
        return None


def added_lines(root: Path, base: str | None, changed: list[str]) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    tracked_diff = git(root, "diff", "-U0", "--no-color", base)[1] if base else ""
    cur_file, line_no = None, 0
    for line in tracked_diff.splitlines():
        if line.startswith("+++ "):
            cur_file = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and cur_file:
            out.append((cur_file, line_no, line[1:]))
            line_no += 1
    untracked = set(git(root, "ls-files", "--others", "--exclude-standard")[1].splitlines())
    if not base:
        untracked |= set(git(root, "diff", "--cached", "--name-only")[1].splitlines())
    for rel in sorted(untracked & set(changed)):
        try:
            for i, text in enumerate((root / rel).read_text(errors="replace").splitlines(), 1):
                out.append((rel, i, text))
        except (OSError, IsADirectoryError):
            pass
    return out


def guard_suppress(root: Path, cfg: dict, base: str | None, changed: list[str] | None) -> tuple[str, str]:
    if changed is None:
        return "skip", "not a git repository"
    skip_globs = cfg.get("gate", {}).get("suppress_ignore", []) + [f"{VENDOR_DIR}/**"]
    fails, warns = [], []
    for rel, n, text in added_lines(root, base, changed):
        if any(fnmatch.fnmatch(rel, g) for g in skip_globs):
            continue
        ext = Path(rel).suffix
        lang = next((name for name, exts in LANG_EXT.items() if ext in exts), None)
        if lang is None:
            continue
        loc = f"{rel}:{n}: {text.strip()[:120]}"
        if any(re.search(p, text) for p in NEVER_ALLOWED):
            fails.append(loc + "   <- focused test, never allowed")
        elif any(re.search(p, text) for p in SUPPRESSIONS[lang]):
            (warns if ALLOW_RE.search(text) else fails).append(loc)
    msg = []
    if fails:
        msg.append("new suppressions without a reason. Fix the cause, or if the suppression is truly right, "
                   "add `gate-allow: <reason>` on the same line for the reviewer:\n  " + "\n  ".join(fails))
    if warns:
        msg.append("suppressions with a stated reason (reviewer must look):\n  " + "\n  ".join(warns))
    return ("fail" if fails else "warn" if warns else "pass"), "\n".join(msg)


# ---------------------------------------------------------------- output parsers

def parse_dryer(code: int, out: str) -> tuple[int, str]:
    dups = [line for line in out.splitlines() if line.startswith("DUPLICATE")]
    if code == 0 and dups:
        return 1, f"{len(dups)} duplicate candidate(s)"
    return code, ""


PARSERS = {"dryer": parse_dryer}


# ---------------------------------------------------------------- runner

def build_checks(cfg: dict) -> list[Check]:
    checks = {c.id: c for c in builtin_checks()}
    for cid, over in cfg.get("check", {}).items():
        if cid in checks:
            c = checks[cid]
            if "cmd" in over:
                c.variants = [(over.get("tool", c.variants[0][0] if c.variants else shlex.split(over["cmd"])[0]), over["cmd"])]
            for k in ("enabled", "tier", "requires"):
                if k in over:
                    setattr(c, k, over[k])
        else:  # custom check
            if "cmd" not in over:
                print(f"[eng-gates] custom check {cid} needs cmd", file=sys.stderr)
                sys.exit(2)
            checks[cid] = Check(cid, over.get("lang", "any"), over.get("tier", "full"), over.get("desc", "custom"),
                                [(over.get("tool", shlex.split(over["cmd"])[0]), over["cmd"])],
                                requires=over.get("requires", []), enabled=over.get("enabled", True))
    return list(checks.values())


def plan(root: Path, cfg: dict, tier: str, all_files: bool, base_override: str | None):
    langs = detect_languages(root, cfg)
    base = resolve_base(root, cfg, base_override)
    changed = changed_files(root, base)
    scope_changed = (not all_files) and changed is not None
    touched = langs_touched(changed) if scope_changed else set(langs)
    files = {}
    for lang in LANG_EXT:
        if scope_changed:
            files[lang] = [f for f in changed if Path(f).suffix in LANG_EXT[lang]]
        else:
            files[lang] = None  # whole tree
    ub = cfg.get("ub", {})
    ctx = {"files": files, "changed_flag": "--changed" if scope_changed else "",
           "crap": ub.get("crap", 10), "dry": ub.get("dry", 0.8),
           "src": cfg.get("gate", {}).get("ts_src", "src")}
    tiers = {"fast"} if tier == "fast" else {"fast", "full"}
    selected = []
    for c in build_checks(cfg):
        if c.tier not in tiers:
            continue
        if c.lang != "any" and c.lang not in langs:
            continue
        selected.append(c)
    return dict(langs=langs, base=base, changed=changed, scope_changed=scope_changed,
                touched=touched, ctx=ctx, checks=selected)


def run_check(root: Path, cfg: dict, c: Check, p: dict, strict: bool, timeout: int) -> dict:
    res = {"id": c.id, "desc": c.desc, "status": "skip", "seconds": 0.0, "cmd": "", "detail": ""}
    if not c.enabled:
        res["detail"] = f"disabled (enable in {CONFIG_NAME}: [check.\"{c.id}\"] enabled = true)"
        return res
    if c.internal:
        t0 = time.monotonic()
        fn = guard_config if c.internal == "config" else guard_suppress
        res["status"], res["detail"] = fn(root, cfg, p["base"], p["changed"])
        res["seconds"] = round(time.monotonic() - t0, 2)
        return res
    if c.lang != "any" and p["scope_changed"] and c.lang not in p["touched"]:
        res["detail"] = "no changed files for this language"
        return res
    if c.lang == "any" and p["scope_changed"] and not p["changed"]:
        res["detail"] = "nothing changed"
        return res
    if c.requires and not any_exists(root, c.requires):
        res["detail"] = f"not configured (needs one of: {', '.join(c.requires)})"
        return res
    chosen = None
    for tool, template in c.variants:
        path = resolve_tool(root, tool, cfg)
        if path:
            chosen = (tool, path, template)
            break
    if not chosen:
        tools = " or ".join(t for t, _ in c.variants)
        res["status"] = "fail" if strict else "skip"
        res["detail"] = f"missing tool: {tools} (scripts/install-tools.sh, or [tools] in {CONFIG_NAME})"
        return res
    tool, path, template = chosen
    argv = render(template, p["ctx"])
    if argv is None:
        res["detail"] = "no matching changed files"
        return res
    if argv and argv[0] == tool:
        argv[0] = path
    res["cmd"] = " ".join(argv)
    t0 = time.monotonic()
    code, out = sh(argv, root, timeout=timeout)
    res["seconds"] = round(time.monotonic() - t0, 1)
    note = ""
    if c.parse in PARSERS:
        code, note = PARSERS[c.parse](code, out)
    res["exit"] = code
    if code == 0:
        res["status"] = "pass"
    else:
        res["status"] = "fail"
        res["detail"] = ((note + "\n") if note else "") + tail(out)
    return res


def cmd_run(root: Path, args) -> int:
    cfg = load_config(root)
    gate_cfg = cfg.get("gate", {})
    p = plan(root, cfg, args.tier, args.all, args.base)
    if not p["langs"]:
        print("[eng-gates] no Rust, Python or JS/TS project found here", file=sys.stderr)
        return 2
    strict = args.strict or gate_cfg.get("strict", False) or bool(os.environ.get("CI"))
    timeout = int(gate_cfg.get("timeout", 900))
    only = set(args.only.split(",")) if args.only else None
    results = []
    for c in p["checks"]:
        if only and c.id not in only and c.id.split(".")[0] not in only:
            continue
        r = run_check(root, cfg, c, p, strict, timeout)
        results.append(r)
        if not args.json:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip", "warn": "WARN"}[r["status"]]
            extra = "" if r["status"] in ("fail", "warn") else (r["detail"] if r["status"] == "skip" else "")
            print(f"  {mark} {r['id']:<15} {r['seconds']:>6}s  {extra}".rstrip(), flush=True)
    failed = [r for r in results if r["status"] == "fail"]
    summary = {
        "tier": args.tier, "ok": not failed, "root": str(root), "languages": p["langs"],
        "base": p["base"], "scope": "changed" if p["scope_changed"] else "all",
        "changed_files": len(p["changed"]) if p["changed"] is not None else None,
        "counts": {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "warn", "skip")},
        "results": results,
    }
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    (state / "last.json").write_text(json.dumps(summary, indent=2))
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for r in results:
            if r["status"] in ("fail", "warn") and r["detail"]:
                print(f"\n--- {r['status'].upper()} {r['id']}" + (f"  ({r['cmd']})" if r["cmd"] else ""))
                print(r["detail"])
        k = summary["counts"]
        if p["scope_changed"] and p["base"] and git(root, "rev-parse", "HEAD")[1].strip() == p["base"]:
            print("\nnote: base equals HEAD, so only uncommitted changes were checked. Work on a branch, "
                  "or pass --base <ref> to cover committed work.")
        print(f"\neng-gates {args.tier}: {'OK' if not failed else 'FAILED'} - "
              f"{k['pass']} pass, {k['fail']} fail, {k['warn']} warn, {k['skip']} skip "
              f"[{summary['scope']}, base {str(p['base'])[:12]}]  details: {STATE_DIR}/last.json")
    return 1 if failed else 0


def cmd_doctor(root: Path) -> int:
    cfg = load_config(root)
    langs = detect_languages(root, cfg)
    base = resolve_base(root, cfg, None)
    print(f"root      {root}\nconfig    {CONFIG_NAME} {'found' if (root / CONFIG_NAME).exists() else 'MISSING (run init)'}")
    print(f"languages {', '.join(langs) or 'none detected'}\nbase      {base or 'none (not git or no commits)'}\n")
    missing = 0
    for c in build_checks(cfg):
        if c.lang != "any" and c.lang not in langs:
            continue
        if c.internal:
            print(f"  ok   {c.id:<15} built-in")
            continue
        if not c.enabled:
            print(f"  off  {c.id:<15} disabled")
            continue
        if c.requires and not any_exists(root, c.requires):
            print(f"  --   {c.id:<15} not configured: needs {' | '.join(c.requires)}")
            continue
        found = [(t, resolve_tool(root, t, cfg)) for t, _ in c.variants]
        hit = next(((t, pth) for t, pth in found if pth), None)
        if hit:
            print(f"  ok   {c.id:<15} {hit[1]}")
        else:
            missing += 1
            print(f"  MISS {c.id:<15} install: {' or '.join(t for t, _ in found)}")
    print(f"\n{missing} check(s) missing a tool. scripts/install-tools.sh installs them.")
    return 0


TEMPLATE_MAP = {
    "common": [("gates.toml", "gates.toml"), ("justfile", "justfile.gates"), ("AGENTS.gates.md", "AGENTS.gates.md")],
    "rust": [("clippy.toml", "clippy.toml"), ("deny.toml", "deny.toml"), ("cargo-lints.toml", ".gates/merge-into-Cargo.toml")],
    "python": [("ruff.toml", "ruff.toml"), (".importlinter", ".importlinter"), ("pyrightconfig.json", "pyrightconfig.json")],
    "ts": [("eslint.config.mjs", "eslint.config.mjs"), (".dependency-cruiser.cjs", ".dependency-cruiser.cjs"),
           ("knip.json", "knip.json"), ("tsconfig.strict.json", "tsconfig.strict.json")],
}


def guess_python_package(root: Path) -> str | None:
    for base in (root / "src", root):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "__init__.py").exists() and d.name not in ("tests", "test", "benches"):
                    return d.name
    return None


def cmd_init(root: Path, force: bool) -> int:
    cfg = load_config(root)
    langs = detect_languages(root, cfg)
    tdir = SKILL_DIR / "templates"
    created, kept = [], []
    pkg = guess_python_package(root) or "your_package"
    for group in ["common", *langs]:
        for src_name, dst_name in TEMPLATE_MAP.get(group, []):
            src, dst = tdir / group / src_name, root / dst_name
            if dst.exists() and not force:
                kept.append(dst_name)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = src.read_text().replace("{{package}}", pkg)
            dst.write_text(text)
            created.append(dst_name)
    vend = root / VENDOR_DIR
    vend.mkdir(parents=True, exist_ok=True)
    for name in ("gate.py", "bench_compare.py", "install-tools.sh"):
        shutil.copy2(SKILL_DIR / "scripts" / name, vend / name)
    created.append(f"{VENDOR_DIR}/ (runner vendored so CI can run it)")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for entry in (f"{STATE_DIR}/last.json", f"{STATE_DIR}/approved.json", ".metrics/"):
        if entry not in lines:
            lines.append(entry)
    gi.write_text("\n".join(lines) + "\n")
    print("created:\n  " + "\n  ".join(created))
    if kept:
        print("kept existing (compare with templates by hand):\n  " + "\n  ".join(kept))
    print("\nnext:")
    if "rust" in langs:
        print("  - merge .gates/merge-into-Cargo.toml into Cargo.toml, then delete it")
    if "python" in langs and pkg == "your_package":
        print("  - set root_package in .importlinter")
    if "ts" in langs:
        print("  - add `\"extends\": \"./tsconfig.strict.json\"` to tsconfig.json; set layer paths in .dependency-cruiser.cjs")
    print("  - append AGENTS.gates.md to AGENTS.md; merge justfile.gates into your justfile")
    print(f"  - CI: copy {SKILL_DIR / 'templates/common/ci-github.yml'} to .github/workflows/gates.yml")
    print("  - run: python3 tools/eng-gates/gate.py doctor   then commit everything (a human commit = approval)")
    return 0


def cmd_approve(root: Path) -> int:
    if os.environ.get("ENG_GATES_AGENT"):
        print("[eng-gates] approve is for humans; refusing inside an agent session (ENG_GATES_AGENT is set)", file=sys.stderr)
        return 2
    cfg = load_config(root)
    p = approval_path(root)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(protected_state(root, cfg), indent=2, sort_keys=True))
    print(f"approved current protected config -> {p.relative_to(root)} (ignored when CI is set)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="gate.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fast", "full", "doctor", "init", "approve"])
    ap.add_argument("--all", action="store_true", help="check the whole tree, not only changed files")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of text")
    ap.add_argument("--base", help="git ref to compare against (default: gates.toml base, then origin/main)")
    ap.add_argument("--only", help="comma list of check ids or prefixes (rust, py, ts, ub, guard)")
    ap.add_argument("--strict", action="store_true", help="a missing tool fails instead of skipping")
    ap.add_argument("--force", action="store_true", help="init: overwrite existing files")
    ap.add_argument("-C", dest="cwd", default=".", help="project directory")
    args = ap.parse_args()
    root = find_root(Path(args.cwd))
    if args.command == "doctor":
        return cmd_doctor(root)
    if args.command == "init":
        return cmd_init(root, args.force)
    if args.command == "approve":
        return cmd_approve(root)
    args.tier = args.command
    return cmd_run(root, args)


if __name__ == "__main__":
    sys.exit(main())
