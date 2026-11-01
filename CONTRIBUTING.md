# Contributing

The rules are the product, so the bar is: **does this change make a model cheaper or safer
without making it wrong?**

## Ground rules

1. **Edit `core/`, never `dist/`.** `dist/` is generated. Change `core/base.md`,
   `core/agent.md`, or `core/expanded.md`, then run `./build.sh`.
2. **Respect the token budget.** `bash tests/run.sh` fails if a tier outgrows its ceiling. If
   you need more tokens, remove something else or argue for raising the ceiling in the PR —
   and say what the extra tokens buy.
3. **Test behavior, not prose.** If you add a rule, add a test that would fail without it.
4. **No unverifiable claims.** If you write "saves 70 %", the test suite has to be able to
   check it. Prefer measured numbers from `./build.sh --stats`.

## Workflow

```bash
./build.sh            # regenerate dist/
./build.sh --check    # must pass: dist/ in sync with core/
bash tests/run.sh     # must pass in full
```

Commit `core/` and `dist/` together. CI runs `./build.sh --check` and the suite on Linux and
macOS, so a PR that forgets to rebuild is caught.

## Changing the rules

- Keep the section structure (Precedence, Library first, Spend tokens like money, …). Adapters
  compose the same text; renaming a heading renames it everywhere.
- One rule per sentence. If you need "and also", it is probably two rules.
- Never compress: safety caveats, data-loss warnings, or the instruction to verify work.
- A rule that only applies to tool-using agents belongs in `core/agent.md`; a rule that only
  applies to chat belongs in `core/expanded.md`.

## Adding an agent target

Two edits, no new copy of the rules:

1. `build.sh` — one `emit` (plain Markdown) or `emit_yaml` (needs frontmatter) line.
2. `install.sh` — one `describe` case entry and one `resolve` case entry, plus a `--list`
   expectations update if you touched the table.

Then extend `tests/run.sh`: assert the file is generated, that its format is valid, and —
if the tool documents one — that the install path is right. Link the tool's own documentation
in the PR so reviewers can verify the path and frontmatter.

## Adding a compressor rule

`compress.sh` is a regex rewriter and is easy to make destructive. Requirements for any new
rule:

- A golden test in `tests/run.sh` for input **and** expected output.
- The output must be idempotent (compressing twice equals compressing once).
- It must not touch fenced code, inline code, or leading Markdown structure.
- It must not leave dangling connectives, unbalanced brackets, or trailing arrows.

## Pull requests

- One concern per PR.
- Say what changed and *how you know it works* — test output, not adjectives.
- Breaking changes to file paths or formats bump the major version in `VERSION` and get a
  `CHANGELOG.md` entry.
