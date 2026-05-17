# Notes for AI coding agents working in this repo

Short, agent-facing guidance. Human-facing dev docs live in `CONTRIBUTING.md`
and `README.md`.

## Testing

- **Do not run the full pytest suite locally.** It's ~3000 tests and
  takes ~8 min on a fast box and >10 min on slower Windows machines.
  Let GitHub CI cover the full sweep.
- **Run targeted test files** for whatever you changed (the test files
  that exercise the modified modules, plus any obvious neighbours).
  Open the PR and watch CI for the broad regression.
- On Windows, always pass `-p no:timeout` — pytest-timeout uses
  `signal.SIGALRM`, which is POSIX-only.
- Known flake: `tests/test_websocket.py::TestWebSocket::test_reflashed_device_empty_token_gets_readopted`
  sometimes hits a 60-second setup timeout. If it's the only failure on
  a PR, rerun the failed job (`gh run rerun <run-id> --failed`) rather
  than investigating a real regression.

## Templates: HTML-attribute escaping for inline JS

If you find yourself writing an `onclick="fn('{{ x }}')"` in a Jinja
template where `x` is **user-typed text** (a name, filename, email,
URL, etc.), use this pattern instead:

```jinja
onclick="fn('{{ x.id }}', {{ x.name | tojson | forceescape }})"
```

`tojson | forceescape` is the safe shape — JSON-quoted (`"`), then
HTML-encoded (`&#34;`) so it survives both layers of decoding intact.
A bare `'{{ x.name }}'` inside a double-quoted attribute breaks for
any name containing a literal `'` (Jinja autoescapes to `&#39;`, the
browser HTML-decodes back to `'`, JS parser bails with
`SyntaxError: missing ) after argument list` at element insertion
time, the onclick is never wired up, and the button silently does
nothing). UUIDs / hex IDs are safe to leave as `'{{ x.id }}'`.

Real bugs that have shipped from this pattern:

- PR #589 — webpage asset "Edit URL" kebab inert on any URL with a `'`
- PR #590 — devices kebab Update / Reboot / etc. inert for `Mia's pi5`
- PR #591 — sweep fix across assets / users / slideshow builder / asset row

## PR workflow

- Auto-merge is enabled on this repo — push, open the PR, and
  `gh pr merge <N> --squash --auto --delete-branch`. CI gates the merge.
- After opening any auto-merge PR, start a watcher script that exits on
  merge or check failure so you receive a completion notification
  (see the user's "PR Watcher Pattern" in their global instructions).
- **Watcher gotcha on this repo:** after every merge to `main`, a
  `github-actions[bot]` immediately pushes a `chore: bump version
  [skip ci]` commit. The `Publish & Deploy` workflow is triggered via
  `workflow_run` after `Smoke Test` succeeds, and GitHub Actions
  records its `headSha` as the latest `main` commit at trigger time —
  which is the bump commit, NOT your merge commit. A SHA-filter-only
  watcher (like the default `watch-pr-to-prod` template) will never see
  the deploy run and time out at exit code 5 even though prod deployed
  fine. Workaround: match `Publish & Deploy` runs that have `headSha`
  *equal to* or *one commit ahead of* the merge SHA (the bump is always
  a single commit on top).
