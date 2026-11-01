# Before / after

Every prompt-compression row below is **real output** from `./compress.sh`, checked by
`tests/run.sh` so it cannot drift. Response examples are illustrative — how much a model
actually saves depends on the model, and pinning made-up percentages to them would be a lie.

## Prompt compression (`./compress.sh`)

| Input | Output |
|-------|--------|
| "Please write me a function that takes a list of users and returns only the active ones" | `fn: users → only the active ones` |
| "Could you write a function that takes an array of numbers and returns the sum" | `fn: numbers → the sum` |
| "Can you create a React component that displays a table of products with sorting" | `React: a table of products with sorting` |
| "I would like you to help me make an API endpoint that handles authentication" | `API endpoint: authentication` |
| "Hi there, can you please just quickly fix the bug in server.js?" | `quickly fix the bug in server.js?` |
| "In order to fix this, I would like you to just remove the extra spaces" | `to fix this, remove the extra spaces` |

Code survives untouched — the part v1 broke:

````console
$ ./compress.sh 'Please fix this:

```python
def f(x):
    # just return really fast
    return x
```'
fix this:

```python
def f(x):
    # just return really fast
    return x
```
````

## Response shape

What the rules ask for, on the same three tasks:

**"Sort an array of objects by date"**

```js
arr.toSorted((a, b) => Date.parse(a.date) - Date.parse(b.date));
```

Not a JSDoc-annotated helper with an `Array.isArray` guard for a problem the caller owns.

**"Remove duplicates from a list" (Python)**

```python
list(dict.fromkeys(items))
```

Order-preserving. `list(set(items))` only if order genuinely does not matter.

**"Validate nested objects in TypeScript"**

```
Deps: zod — npm i zod
```

The library *is* the answer. Anything longer is a re-implementation.

## The other half: agent mode

Token efficiency in an agent is not short answers — it is fewer wasted turns:

| Wasteful behavior | What LazyEng asks for |
|-------------------|-----------------------|
| Rewrites a whole file to change one line | Smallest targeted edit |
| Re-reads a file it just wrote | Does not re-read |
| "I've made the following changes: …" | The diff is the report |
| "This should work now" | Runs the project's own checks, reports `pass` / `fail: <reason>` |
| Commits and pushes unprompted | Stops when the task is done |
| `git push --force` to "clean up" | States the command and waits for a yes |
