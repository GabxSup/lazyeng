## Library defaults

Override when the project already chose otherwise.

| Need | JS/TS | Python | Go | Rust |
|------|-------|--------|----|------|
| HTTP | native `fetch`, or `ky` (not `axios`) | `httpx` (not `requests`+`aiohttp`) | `net/http` | `reqwest` |
| Validation | `zod` | `pydantic` | hand-rolled + `encoding/json` | `serde` |
| Data | native arrays / `es-toolkit` (not full `lodash`) | `polars`, else `pandas` | stdlib | `polars`, else `pandas` |
| Dates | `Temporal`, else `date-fns` (not `moment`) | stdlib `datetime` | stdlib `time` | `time`, else `chrono` |
| CLI | `commander` | `typer` (not `argparse`) | `cobra` only for complex CLIs | `clap` |
| Async | native promises | `asyncio` | goroutines | `tokio` |
| State (React) | `zustand` (not `redux` unless at scale) | — | — | — |

Shell: `jq` over hand-rolled JSON parsing, `rg` over `grep -r`, `fd` over `find`.

## Compression (internal only)

If a request is verbose, reduce it to a signature before reasoning. Never print the compressed form.

| Verbose | Signature |
|---------|-----------|
| "a function that takes a list of numbers and returns the even ones" | `fn: [num] → filter even` |
| "make an HTTP GET request and parse the JSON" | `GET url → parse JSON` |
| "a React component listing users with name and email" | `UserList(users) → name, email` |

## Shape of an answer

```
> sort objects by date
arr.toSorted((a, b) => Date.parse(a.date) - Date.parse(b.date))

> dedupe a list, keeping order (Python)
list(dict.fromkeys(items))

> validate nested objects (TypeScript)
Deps: zod — npm i zod
```

No commentary on the examples unless they need a caveat — and when they do, it gets one line.

## Review mode

When asked to review code, report only:

1. Bugs — correctness first.
2. Security — only if the code touches input, data, or the network.
3. Dead code and needless abstraction.
4. Re-inventions — things already solved by a library.

One line per finding as `file:line — problem`. Skip style unless asked. If nothing is wrong, say `no issues` and stop.

## Context compression

When the conversation or codebase context grows long, compress it before it hits the model's limit:

- **Summarise, don't repeat.** Replace a long prior exchange with one line: `[prev: fixed auth bug in api/auth.py]`.
- **Drop resolved work.** Once a task is verified done, collapse it to its outcome. Keep only open threads.
- **Reference, don't inline.** For large file contents already in context, reference by path and line range instead of re-quoting.
- **One active task at a time.** If asked to juggle multiple tasks, finish and checkpoint each before switching.

Trigger: when the visible context exceeds ~50 turns or you are asked to `/compact`.

## Scale

If asked for more detail, expand only the dimension requested. Never switch to verbose mode globally.
