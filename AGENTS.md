# AGENTS.md — working on LazyEng

This repository ships an AI ruleset. Treat the rules as code: they have sources, a build, and
a test suite. The rules we ship also apply to work in this repo — read
[`dist/AGENTS.md`](dist/AGENTS.md) if you want the short version.

## Before you change anything

- `core/` is the product. `dist/` is generated output — never edit it by hand.
- Run `./build.sh` after touching `core/`, otherwise `./build.sh --check` and CI fail.
- Read `README.md` first. If you change behavior, the claim in the README changes with it, in
  the same commit.
- Adding a rule costs tokens on every request. `bash tests/run.sh` enforces a ceiling; if you
  need room, remove a rule rather than raising the ceiling.

## Commands

```bash
./build.sh            # regenerate dist/
./build.sh --check    # CI gate: dist/ in sync with core/
./build.sh --stats    # sizes and token estimates
bash tests/run.sh     # full suite — must pass
./install.sh --list   # install targets
```

## Rules for changes here

- **Small, targeted diffs.** No drive-by reformatting of `core/` or the scripts.
- **One concern per commit.** Rule changes, build changes, and docs changes are separate.
- **Test the behavior.** A new compressor rule needs a golden case in `tests/run.sh` and must
  be idempotent. A new adapter needs a `--check`-clean `dist/` entry and a format assertion.
- **Never weaken the safety rules** in `core/agent.md` (irreversible actions, secrets,
  verification). Those are load-bearing.
- **No unverifiable claims** in docs. "Saves 70 %" must come from `./build.sh --stats` and be
  checkable by the suite.
- **Do not commit** generated noise, editor files, or `.DS_Store`.

## Where things live

| Path | Role |
|------|------|
| `core/base.md` | rules every surface gets |
| `core/agent.md` | tool-using agent behavior |
| `core/expanded.md` | detail, defaults, examples — loaded on demand only |
| `build.sh` | composes `dist/` from `core/` |
| `install.sh` | installs an adapter into an agent's instruction file |
| `compress.sh` | prompt compressor (perl, lossy, code-safe) |
| `tests/run.sh` | the suite CI runs |
| `examples/` | measured before/after samples |
