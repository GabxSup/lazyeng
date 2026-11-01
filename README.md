# LazyEng `v2.0.0`

**Token-efficient coding rules that install into the agent you already use.**

One ruleset — no filler output, library-first choices, minimal diffs, verification before
"done" — rendered into the instruction-file format of Claude Code, Codex, Cursor, Copilot,
Gemini CLI, Windsurf, Cline, Aider, and any Agent Skills client. No runtime, no network —
just `bash` and `perl`, which ship with macOS and virtually every Linux.

> The best code is the code you don't write.

---

## Install

```bash
git clone https://github.com/GabxSup/lazyeng && cd lazyeng

./install.sh                          # AGENTS.md in the current directory
./install.sh --target claude          # CLAUDE.md
./install.sh --target cursor --dry-run
./install.sh --target claude --scope global
./install.sh --list                   # every target and its path
```

`install.sh` merges into files you already own instead of replacing them, using
`<!-- lazyeng:begin -->` … `<!-- lazyeng:end -->` markers. Your own instructions survive, a
second run is a no-op, and `--uninstall` removes exactly what it added.

### Chat only?

Paste a prompt file into any model's system prompt — no tooling required:

| File | Use |
|------|-----|
| [`dist/system-prompt.md`](dist/system-prompt.md) | full ruleset, paste into a chat UI |
| [`dist/system-prompt-compact.md`](dist/system-prompt-compact.md) | ~650 tokens, for an API `system` field |

---

## Supported agents

| Target | Installs to | Read by |
|--------|-------------|---------|
| `agents` | `AGENTS.md` | [Codex, Cursor, Copilot, Gemini CLI, Zed, Amp, opencode](https://agents.md) |
| `claude` | `CLAUDE.md` | Claude Code |
| `claude-skill` | `.claude/skills/lazyeng/SKILL.md` | [Claude Code skills](https://code.claude.com/docs/en/skills) |
| `gemini` | `GEMINI.md` | [Gemini CLI](https://geminicli.com/docs/cli/gemini-md/) |
| `copilot` | `.github/copilot-instructions.md` | GitHub Copilot |
| `cursor` | `.cursor/rules/lazyeng.mdc` | [Cursor project rules](https://cursor.com/docs/rules) |
| `windsurf` | `.windsurf/rules/lazyeng.md` | Windsurf rules |
| `cline` | `.clinerules/lazyeng.md` | [Cline rules](https://docs.cline.bot/customization/cline-rules) |
| `aider` | `CONVENTIONS.md` | Aider |

Every generated file is also committed under [`dist/`](dist), so you can copy one by hand or
`curl` a single raw file if you'd rather not clone.

Formats follow each tool's current docs: Cursor requires the `.mdc` extension plus
frontmatter (`alwaysApply: true`), Windsurf requires `trigger: always_on`, and the skill ships
YAML frontmatter that validates against the [Agent Skills specification](https://agentskills.io/specification).
Global installs (`--scope global`) are offered only where a tool documents a home-directory
file — use `--dry-run` to see the exact path before writing anything.

---

## What you get

```
## Precedence
1. The user's instruction.
2. The project's own rules and style.
3. These rules.
```

That ordering is the point. LazyEng never overrides your `AGENTS.md`, your linter, or your
conventions — it only governs *how much* the model writes.

- **Library first**, with a guard against invented package names.
- **No preamble, no sign-off, no change-summary.** The diff is the report.
- **Minimal diffs.** No drive-by refactors; targeted edits over rewrites.
- **Verify, don't assert.** Run the project's own checks, then report `pass` or `fail: <reason>`.
- **Irreversible needs a yes.** Deleting data, force-push, dropping a database, installing
  untrusted code.
- **Brevity has limits.** Safety warnings, skipped setup, and real uncertainty are never compressed.

---

## Token budget

| Tier | File | Est. tokens |
|------|------|-------------|
| Chat, full | `dist/system-prompt.md` | ~1 200 |
| API `system` field | `dist/system-prompt-compact.md` | ~650 |
| Always-on agent file | `dist/AGENTS.md` | ~1 020 |
| Skill (loaded on demand) | `dist/skills/lazyeng/SKILL.md` | ~1 650 |

Estimates are `chars / 4`. Run `./build.sh --stats` for the current numbers — the test suite
fails if any tier outgrows its budget.

That is deliberately *larger* than v1's ~450-token file, which had no precedence rules and no
agent guidance. The extra tokens are the part that stops an agent from doing damage.

---

## Built from one source

| Generated file | Composed from |
|----------------|---------------|
| `dist/system-prompt.md` | `base` + `expanded` |
| `dist/system-prompt-compact.md` | `base` |
| `dist/AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `copilot-instructions.md`, `cursor/`, `windsurf/`, `cline/`, `CONVENTIONS.md` | `base` + `agent` |
| `dist/skills/lazyeng/SKILL.md` | `base` + `agent` + `expanded` |

Always-on files get `base` + `agent` because every request pays for them. The skill is loaded
only when used, so it carries the full text.

`core/` is the only hand-written copy of the rules. `./build.sh` composes every adapter from
it, `./build.sh --check` fails when `dist/` drifts, and `bash tests/run.sh` covers build
determinism, adapter formats, composition, token budgets, version sync, the compressor, and
the installer.

```bash
./build.sh            # regenerate dist/
./build.sh --check    # CI: is dist/ in sync?
./build.sh --stats    # size and token estimates
bash tests/run.sh     # full suite
./install.sh --list   # install targets
```

Adding an agent means one `emit` line in `build.sh` and one `resolve` case in `install.sh` —
not a new copy of the rules.

---

## compress.sh

Shrinks a verbose prompt before you send it. Rewrites prose only: fenced code blocks and
inline `` `code` `` pass through byte-for-byte.

```bash
$ echo "Please write me a function that takes a list and returns only the unique items" | ./compress.sh
fn: list → only the unique items

$ ./compress.sh "I would like you to help me make an API endpoint that handles authentication"
API endpoint: authentication

$ ./compress.sh --stats "Hi there, can you please just quickly fix the bug in server.js?"
quickly fix the bug in server.js?
chars: 66 -> 31 (53% smaller)
```

It is lossy by design — read the output before sending it. Requires `perl` (preinstalled on
macOS and nearly all Linux); everything else needs only `bash`.

---

## Limitations

- Token counts are a `chars / 4` heuristic, not a tokenizer. Treat them as ±15 %.
- `compress.sh` is a regex rewriter, not a parser. It protects code and Markdown structure,
  but it can flatten nuance in unusual phrasing.
- Global install paths follow each tool's docs as of **2026-11**; tools move these. Check
  `--dry-run`.
- Windows: the scripts are POSIX. They run under WSL or Git Bash; `dist/` files work anywhere.

---

## License

MIT © 2026 Gabriel Mideros Sanchez — see [LICENSE](LICENSE).

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). History: [CHANGELOG.md](CHANGELOG.md).
