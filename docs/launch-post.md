Title: I built LazyEng — free, MIT-licensed rules that install into Claude Code (and 7 other coding agents) to cut token waste

---

**What I built**

I built LazyEng, a free and MIT-licensed set of coding rules for AI agents. It is not a
product with a signup: you clone it, run one installer, and the rules drop into whichever
agent you already use.

**What it does**

The rules are opinionated about the handful of habits that cost the most money and do the
most damage in agent work:

- Don't write a function when a maintained library already does it. If you can't verify the
  package exists, say so instead of inventing an install command.
- No preamble, no "I've made the following changes" summary. The diff is the report.
- Smallest targeted edit. No drive-by refactors or reformatting of unrelated code.
- Run the project's own tests, lint and typecheck, then report `pass` or `fail: <reason>`.
  Never claim something works because it looks right.
- Ask once, when the ambiguity actually changes the outcome. Otherwise take the obvious default.
- Deleting data, force-push, dropping a database, running untrusted code: state the exact
  command and wait for a yes.
- Efficiency has a hard limit. Safety warnings, skipped setup and real uncertainty never get
  compressed.

It also sets the order of authority: your instruction > your project's own AGENTS.md, linter
and code style > these rules. That ordering is the part I care about most, because a ruleset
that overrides the conventions you already have is a bug, not a feature.

**How Claude Code fits in**

I wrote the first version by hand, then rebuilt it with an AI coding agent — not Claude Code,
and I'd rather say that plainly than claim otherwise. What is Claude-specific is the output:
the repo ships a `CLAUDE.md` and a Claude Code skill in the Agent Skills format, and the rules
are shaped around how Claude Code behaves once it has file-edit and shell tools, which is a
different problem from a chat model with no tools. Two rules exist because of things I kept
watching go wrong: the precedence section, and "verify, don't assert" — agents claiming a
change works without ever running the project's tests.

**How it works**

`core/` holds the rules once. `build.sh` composes them into the instruction format each tool
actually requires: `CLAUDE.md`, a Claude Code skill (`SKILL.md`), `AGENTS.md` (Codex, Cursor,
Copilot, Gemini CLI, Zed, Amp, opencode), Cursor's `.mdc` with `alwaysApply`, Windsurf's
`trigger: always_on`, Copilot's `.github/copilot-instructions.md`, `GEMINI.md`, `.clinerules/`,
and Aider's `CONVENTIONS.md`. Editing one source file updates all of them, and CI fails if the
generated files drift.

`install.sh` merges into files you already own using marker blocks instead of overwriting
them, so `--uninstall` restores your file exactly as it was.

There's also `compress.sh` for prompts. It is regex-based and lossy, and it never touches
fenced code or inline `code`:

```
"Could you please write a function that takes a list of users and returns the active ones"
→ fn: users → the active ones
```

**It's free to try — there is no paid tier**

MIT licensed. No account, no telemetry, no upsell:

```
git clone https://github.com/GabxSup/lazyeng
cd lazyeng
./install.sh --list          # every target and where it installs
./install.sh --target claude
```

`--dry-run` prints the exact path before writing anything. Sizes, since that's the point:
~650 tokens for the API system-prompt tier, ~1,000 for an always-on `AGENTS.md`, ~1,650 for
the full skill that only loads when used. Those are `chars/4` estimates, not a real tokenizer,
and the README says so.

**What I want feedback on**

I've tried it with opencode and Antigravity so far. Two things I'm genuinely unsure about:

1. Are the agent rules too strict? "Don't commit unless asked" and "stop when done" may fight
   how people actually work.
2. Is the always-on file too big to justify its place in every request's context?

Repo: https://github.com/GabxSup/lazyeng
