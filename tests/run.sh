#!/usr/bin/env bash
# tests/run.sh — the whole test suite. No dependencies beyond bash, perl, awk, git.
#
#   bash tests/run.sh

set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1

pass=0
fail=0
failed_names=""

ok()   { pass=$((pass + 1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { fail=$((fail + 1)); failed_names="$failed_names\n  - $1"; printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; }
group() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# assert_eq <name> <expected> <actual>
assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else
        bad "$1" "expected: [$2]  actual: [$3]"
    fi
}

assert_contains() {
    case $3 in
        *"$2"*) ok "$1" ;;
        *) bad "$1" "missing [$2] in $3" ;;
    esac
}

assert_exit() { # assert_exit <name> <expected code> <command...>
    name=$1; want=$2; shift 2
    "$@" >/dev/null 2>&1
    got=$?
    assert_eq "$name" "$want" "$got"
}

est_tokens() { # chars/4 heuristic used by build.sh --stats
    c=$(wc -c < "$1" | tr -d ' ')
    echo $((c / 4))
}

TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT

# ── build ────────────────────────────────────────────────────────────────────
group "build"
assert_exit "build.sh runs" 0 ./build.sh
assert_exit "build.sh --check passes on a fresh build" 0 ./build.sh --check

missing=""
for f in system-prompt.md system-prompt-compact.md AGENTS.md CLAUDE.md GEMINI.md \
         copilot-instructions.md CONVENTIONS.md cursor/lazyeng.mdc windsurf/lazyeng.md \
         cline/lazyeng.md skills/lazyeng/SKILL.md README.md; do
    [ -s "dist/$f" ] || missing="$missing $f"
done
assert_eq "every adapter is generated and non-empty" "" "$missing"

# dist/ must be reproducible: a second build must not change anything.
before=$(find dist -type f -exec cksum {} + | sort)
./build.sh >/dev/null
after=$(find dist -type f -exec cksum {} + | sort)
assert_eq "build is deterministic" "$before" "$after"

# ── composition matrix ───────────────────────────────────────────────────────
group "composition"
assert_contains "AGENTS.md has agent mode" "## Agent mode" "$(cat dist/AGENTS.md)"
assert_contains "CLAUDE.md has agent mode" "## Agent mode" "$(cat dist/CLAUDE.md)"
assert_contains "AGENTS.md has precedence" "## Precedence" "$(cat dist/AGENTS.md)"
assert_contains "AGENTS.md has safety rails" "Irreversible needs a yes" "$(cat dist/AGENTS.md)"
assert_contains "SKILL.md is the full text" "## Library defaults" "$(cat dist/skills/lazyeng/SKILL.md)"
assert_contains "system-prompt.md is chat-focused" "## Library defaults" "$(cat dist/system-prompt.md)"
case $(cat dist/system-prompt.md) in
    *"## Agent mode"*) bad "chat prompt omits agent mode" "system-prompt.md should not tell a chat model about tools" ;;
    *) ok "chat prompt omits agent mode" ;;
esac

# ── adapter format validity ──────────────────────────────────────────────────
group "adapter formats"
assert_eq "cursor rule starts with frontmatter" "---" "$(head -1 dist/cursor/lazyeng.mdc)"
assert_contains "cursor rule is alwaysApply" "alwaysApply: true" "$(cat dist/cursor/lazyeng.mdc)"
assert_eq "windsurf rule starts with frontmatter" "---" "$(head -1 dist/windsurf/lazyeng.md)"
assert_contains "windsurf rule is always_on" "trigger: always_on" "$(cat dist/windsurf/lazyeng.md)"
assert_eq "skill starts with frontmatter" "---" "$(head -1 dist/skills/lazyeng/SKILL.md)"
assert_contains "skill name matches its directory" "name: lazyeng" "$(cat dist/skills/lazyeng/SKILL.md)"

skill_desc=$(sed -n 's/^description: //p' dist/skills/lazyeng/SKILL.md | head -1)
[ ${#skill_desc} -gt 20 ] && [ ${#skill_desc} -le 1024 ] \
    && ok "skill description is within the 1024-char spec limit" \
    || bad "skill description is within the 1024-char spec limit" "len=${#skill_desc}"

bad_eof=""
for f in $(find dist -type f); do
    [ -n "$(tail -c 1 "$f")" ] && bad_eof="$bad_eof $f"
done
assert_eq "no file ends mid-line" "" "$bad_eof"

# ── token budget ─────────────────────────────────────────────────────────────
group "token budget"
compact=$(est_tokens dist/system-prompt-compact.md)
full=$(est_tokens dist/system-prompt.md)
agents=$(est_tokens dist/AGENTS.md)
printf '       compact %s · full %s · AGENTS.md %s (est. tokens)\n' "$compact" "$full" "$agents"
[ "$compact" -le 800 ] && ok "compact prompt under 800 est. tokens" \
    || bad "compact prompt under 800 est. tokens" "$compact"
[ "$agents" -le 1200 ] && ok "always-on AGENTS.md under 1200 est. tokens" \
    || bad "always-on AGENTS.md under 1200 est. tokens" "$agents"
[ "$full" -le 1600 ] && ok "full prompt under 1600 est. tokens" \
    || bad "full prompt under 1600 est. tokens" "$full"
[ "$compact" -lt "$full" ] && ok "compact is smaller than full" || bad "compact is smaller than full"

# ── version sync ─────────────────────────────────────────────────────────────
group "version"
version=$(tr -d '[:space:]' < VERSION)
assert_contains "compress.sh version matches VERSION" "$version" "$(./compress.sh --version)"
assert_contains "install.sh version matches VERSION" "$version" "$(./install.sh --version)"
assert_contains "dist/SKILL.md carries the version" "version: \"$version\"" "$(cat dist/skills/lazyeng/SKILL.md)"
assert_contains "README carries the version" "$version" "$(cat README.md)"

# ── compress.sh behaviour ────────────────────────────────────────────────────
group "compress.sh"
assert_exit "perl syntax is valid" 0 perl -c compress.sh
assert_exit "--help exits 0" 0 ./compress.sh --help
assert_exit "--version exits 0" 0 ./compress.sh --version
assert_eq "empty stdin produces no output" "" "$(printf '' | ./compress.sh)"

check_compress() { # check_compress <input> <expected>
    assert_eq "compress: $1" "$2" "$(./compress.sh "$1")"
    once=$(./compress.sh "$1")
    assert_eq "idempotent: $1" "$once" "$(./compress.sh "$once")"
}

check_compress "Please write me a function that takes a list and returns only the unique items" \
               "fn: list → only the unique items"
check_compress "Could you please write a function that takes a list of users and returns the active ones" \
               "fn: users → the active ones"
check_compress "Could you write a function that takes an array of numbers and returns the sum" \
               "fn: numbers → the sum"
check_compress "Write me a function that takes a URL and returns parsed JSON" \
               "fn: URL → parsed JSON"
check_compress "Can you create a React component that displays a list of users with their names and emails" \
               "React: users with their names and emails"
check_compress "I would like you to help me make an API endpoint that handles authentication" \
               "API endpoint: authentication"
check_compress "Hi there, can you please just quickly fix the bug in server.js?" \
               "quickly fix the bug in server.js?"
check_compress "Please create a list of files in the directory" \
               "create a list of files in the directory"
check_compress "sort an array of objects by date" \
               "sort an array of objects by date"

# Code must survive untouched — the v1 compressor rewrote comments inside fences.
fenced_in='Please fix this:

```python
def f(x):
    # just return really fast
    return x  # actually
```'
fenced_want='fix this:

```python
def f(x):
    # just return really fast
    return x  # actually
```'
assert_eq "fenced code is passed through byte-for-byte" "$fenced_want" "$(printf '%s\n' "$fenced_in" | ./compress.sh)"

inline='Check the `just_really` and `actually_fine` variables please'
assert_eq "inline code is protected" \
    "Check the \`just_really\` and \`actually_fine\` variables" \
    "$(./compress.sh "$inline")"

bullets='- please just do the thing
- actually do the other thing'
assert_eq "markdown bullets keep their markers and line breaks" \
    "- do the thing
- do the other thing" \
    "$(printf '%s\n' "$bullets" | ./compress.sh)"

# --tokens flag prints estimate to stderr, stdout unchanged
tokens_out=$(./compress.sh --tokens "Please sort the list" 2>/dev/null)
tokens_err=$(./compress.sh --tokens "Please sort the list" 2>&1 >/dev/null)
assert_eq "--tokens stdout is the compressed prompt" "sort the list" "$tokens_out"
assert_contains "--tokens stderr has token estimate" "~tokens:" "$tokens_err"

# --file flag edits a file in place
FILE_TMP=$(mktemp)
printf 'Please just do the thing.\n' > "$FILE_TMP"
./compress.sh --file "$FILE_TMP"
assert_eq "--file rewrites the file" "do the thing." "$(cat "$FILE_TMP")"
rm -f "$FILE_TMP"

# duplicate consecutive paragraphs are collapsed
dup_in='Fix the bug.

Fix the bug.

Add a test.'
dup_want='Fix the bug.

Add a test.'
assert_eq "duplicate paragraphs are collapsed" "$dup_want" "$(printf '%s\n' "$dup_in" | ./compress.sh)"

# new filler words: literally, go ahead and, as you know
assert_eq "filler: literally" \
    "fix this" \
    "$(./compress.sh "literally fix this")"
assert_eq "filler: go ahead and (leading)" \
    "fix this" \
    "$(./compress.sh "go ahead and fix this")"
assert_eq "filler: as you know" \
    "the sky is blue." \
    "$(./compress.sh "as you know, the sky is blue.")"

# ── install.sh behaviour ─────────────────────────────────────────────────────
group "install.sh"
assert_exit "--list exits 0" 0 ./install.sh --list
assert_exit "unknown target is rejected" 2 ./install.sh --target nonsense --dir "$TMP"
assert_exit "corrupt scope is rejected" 2 ./install.sh --target agents --scope sideways --dir "$TMP"

P="$TMP/proj"
mkdir -p "$P"
printf '# House rules\n\nKeep my instructions.\n' > "$P/AGENTS.md"

assert_exit "install into an existing AGENTS.md" 0 ./install.sh --target agents --dir "$P"
assert_contains "block markers are present" "lazyeng:begin" "$(cat "$P/AGENTS.md")"
assert_contains "existing content survived" "Keep my instructions." "$(cat "$P/AGENTS.md")"
assert_eq "block appears exactly once" "1" "$(grep -c 'lazyeng:begin' "$P/AGENTS.md")"

sum1=$(cksum "$P/AGENTS.md")
./install.sh --target agents --dir "$P" >/dev/null
assert_eq "re-install is a no-op" "$sum1" "$(cksum "$P/AGENTS.md")"

# A second target must not disturb the first.
./install.sh --target claude --dir "$P" >/dev/null
assert_eq "AGENTS.md untouched by a CLAUDE.md install" "1" "$(grep -c 'lazyeng:begin' "$P/AGENTS.md")"
[ -f "$P/CLAUDE.md" ] && ok "CLAUDE.md created" || bad "CLAUDE.md created"

# dry-run writes nothing
D="$TMP/dry"
mkdir -p "$D"
./install.sh --target all --dir "$D" --dry-run >/dev/null
assert_eq "dry-run writes nothing" "" "$(find "$D" -type f)"

# owned files are protected unless --force
mkdir -p "$P/.cursor/rules"
printf 'mine\n' > "$P/.cursor/rules/lazyeng.mdc"
assert_exit "refuses to overwrite a file it does not own" 1 ./install.sh --target cursor --dir "$P"
assert_eq "the foreign file is intact" "mine" "$(cat "$P/.cursor/rules/lazyeng.mdc")"
assert_exit "--force overwrites it" 0 ./install.sh --target cursor --dir "$P" --force

# uninstall restores the original file exactly
assert_exit "uninstall succeeds" 0 ./install.sh --target agents --dir "$P" --uninstall
assert_eq "uninstall restores the original file" \
    "# House rules

Keep my instructions." "$(cat "$P/AGENTS.md")"
assert_eq "no markers left behind" "0" "$(grep -c 'lazyeng:begin' "$P/AGENTS.md")"

assert_exit "uninstalling an owned target" 0 ./install.sh --target cursor --dir "$P" --uninstall
[ -f "$P/.cursor/rules/lazyeng.mdc" ] && bad "owned file removed on uninstall" || ok "owned file removed on uninstall"

# clean file that LazyEng created is deleted, not left empty
Q="$TMP/created"
mkdir -p "$Q"
./install.sh --target gemini --dir "$Q" >/dev/null
./install.sh --target gemini --dir "$Q" --uninstall >/dev/null
[ -f "$Q/GEMINI.md" ] && bad "file created by LazyEng is removed on uninstall" || ok "file created by LazyEng is removed on uninstall"

# ── docs ─────────────────────────────────────────────────────────────────────
group "docs"
for f in README.md LICENSE CHANGELOG.md CONTRIBUTING.md AGENTS.md; do
    [ -s "$f" ] && ok "$f exists" || bad "$f exists"
done
assert_contains "LICENSE is MIT" "MIT License" "$(cat LICENSE)"

missing_docs=""
for f in AGENTS.md CLAUDE.md GEMINI.md .github/copilot-instructions.md CONVENTIONS.md \
         .cursor/rules/lazyeng.mdc .windsurf/rules/lazyeng.md .clinerules/lazyeng.md \
         .claude/skills/lazyeng/SKILL.md; do
    grep -qF "$f" README.md || missing_docs="$missing_docs $f"
done
assert_eq "README documents every install destination" "" "$missing_docs"

# ── summary ──────────────────────────────────────────────────────────────────
printf '\n\033[1m%s passed, %s failed\033[0m\n' "$pass" "$fail"
if [ "$fail" -ne 0 ]; then
    printf 'failures:%b\n\n' "$failed_names"
    exit 1
fi
