# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [2.1.0] — 2026-11-08

### Added

- **`compress.sh --tokens`** — prints before/after token estimates (`chars/4`) to stderr alongside the compressed output.
- **`compress.sh --file FILE`** — compresses `FILE` and overwrites it in place; use `-` to read stdin and write stdout (existing behavior).
- **Paragraph deduplication** in `compress_document`: consecutive identical paragraphs (after whitespace normalisation) are collapsed to one copy. Catches copy-paste repetition common in long context pastes.
- **Extended filler vocabulary**: `literally`, `absolutely`, `definitely`, `certainly`, `naturally`, `essentially`, `effectively`, `quite`, `rather`, `somewhat`, `fairly`, `pretty`, `go ahead and`, `feel free to`, `don't hesitate to`, `as you know`, `as mentioned`, `in this case/context/scenario/situation`, `at the end of the day`, `at this point in time`.

### Changed

- **`core/base.md`** trimmed by ~13 %: merged "No preamble" and "diff is the report" bullets; collapsed `Precision`, `Modular`, `No speculation` to tighter phrasing; removed redundant prose in `Precedence` and `Library first`. Always-on `AGENTS.md` now costs ~931 tokens (was ~1 019); compact system prompt costs ~562 tokens (was ~650).
- **`core/expanded.md`** gains a **Context compression** section: rules for summarising resolved work, referencing instead of re-quoting, and triggering on `/compact` or long conversations.

### Fixed

- Idempotency of the `"quickly fix …"` test case: `quickly` is now only stripped as a leading politeness marker, not mid-sentence, so a second pass leaves the output unchanged.

## [2.0.0] — 2026-11-01

The release that turns a prompt file into an installable, agent-compatible plugin.

### Added

- **Agent adapters.** One ruleset now ships as `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
  `.github/copilot-instructions.md`, `.cursor/rules/lazyeng.mdc`, `.windsurf/rules/lazyeng.md`,
  `.clinerules/lazyeng.md`, `CONVENTIONS.md`, and an Agent Skills `SKILL.md` — each in the
  format its tool actually requires (frontmatter, extensions, triggers).
- **`install.sh`.** Installs by target into project or global scope, merges into existing
  instruction files with marker blocks instead of overwriting them, supports `--dry-run`,
  `--uninstall`, `--force`, `--list`, and is idempotent.
- **Agent-mode rules.** Read before edit, change only what the task needs, verify with the
  project's own checks instead of asserting success, stop when done, ask once when genuinely
  blocked, and require explicit confirmation for irreversible actions.
- **Precedence section.** User instruction → project rules → LazyEng. The ruleset can no
  longer quietly override a project's own conventions.
- **"Brevity has limits" guard.** Safety, data-loss, and uncertainty warnings are never
  compressed. Efficiency never applies to the diff itself.
- **Hallucinated-package guard.** If a library's existence is unverified, say so rather than
  emit an invented install command.
- **`build.sh`.** Composes `dist/` from `core/`, with `--check` (fails on drift) and
  `--stats` (size and token estimates).
- **Test suite and CI.** `bash tests/run.sh` covers build determinism, adapter formats,
  composition, token budgets, version sync, compressor output, and installer behaviour;
  GitHub Actions runs it on Linux and macOS.

### Changed

- Renamed to **LazyEng** everywhere. The README said "LazyPrompt", the prompt said "LazyEng".
- Sources moved to `core/` (`base.md`, `agent.md`, `expanded.md`); generated files moved to
  `dist/`. The root-level `system-prompt.md` and `system-prompt-compact.md` are now
  `dist/system-prompt.md` and `dist/system-prompt-compact.md`.
- Rules now apply only where they help: chat surfaces get no agent/tool instructions.
- Documentation claims are measured, not asserted: `README` states the real token tiers and
  the test suite enforces them.

### Fixed

- `compress.sh` left a dangling `takes` and an unbalanced `[`:
  `"…takes a list of users and returns…"` produced `fn: takes [users → …`.
- `compress.sh` rewrote the contents of fenced code blocks and inline code, corrupting the
  very examples people paste.
- `compress.sh` printed a blank line for empty input.
- `examples/token-savings.md` documented outputs `compress.sh` had never produced; the
  examples are now generated from real invocations and checked by tests.
- The project claimed MIT but shipped no `LICENSE` file.

## [1.2.0] — prior

Initial public ruleset: library-first bias, token discipline, language defaults,
anti-patterns, and an early `compress.sh`. Kept as the baseline this release replaces.
