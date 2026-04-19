# Contributing to agora-cms

## Development quickstart

See the top-level `README.md` for local setup, running tests, and the compose
stack used for end-to-end testing.

## Changing the database schema

All schema changes go through Alembic.  `cms/database.py`'s `run_migrations`
just calls `alembic upgrade head`; there is no hand-written DDL to update.

**Workflow when you add or change an ORM model:**

1. Edit the model under `shared/models/` or `cms/models/`.
2. Start a Postgres you can point at (the compose stack works:
   `docker compose up -d db`).
3. Generate a migration:
   ```bash
   AGORA_CMS_DATABASE_URL=postgresql+asyncpg://agora:agora@localhost:5432/agora_cms \
     alembic revision --autogenerate -m "describe your change"
   ```
4. Review the file that lands in `alembic/versions/`.  Autogenerate is
   usually right but never blindly trust it — check that `upgrade()` does
   what you expect, and hand-edit if needed (for example, autogenerate
   cannot detect column renames; it sees a drop + add).
5. Apply locally to sanity-check: `alembic upgrade head`.
6. Commit the model change *and* the new migration file in the same PR.

**Policy:**

- Forward-only.  The generated `downgrade()` raises `NotImplementedError` —
  don't bother filling it in.  If you need to undo something in prod, write
  a new forward migration.
- Never edit an existing migration file after it has merged to `main`.
  Even a trivial edit breaks stamped deployments.  Add a new migration instead.
- Revisions use numeric IDs (`0001`, `0002`, ...).  Pass `--rev-id 000N`
  to `alembic revision` or rename the file after generation.

**What CI enforces:**

- `Alembic Check` runs `alembic upgrade head && alembic check` on a fresh
  Postgres.  It fails if your ORM models would produce any new autogenerate
  operations — i.e. the migration you added doesn't match the model change,
  or you forgot the migration entirely.
- `Migration Safety` runs your migrations against a populated DB seeded
  from main, catching destructive or non-idempotent migrations.

## Pull request flow

1. Branch from `main`.
2. Push your branch and open a PR — do **not** bump `cms/__init__.py`'s
   `__version__`; that is auto-bumped post-deploy.
3. Wait for CI:
   - `CI Gate` — always-pass sentinel (required).
   - `Tests/test` and `Tests/e2e` — the test suite.
   - `Alembic Check` — **required**; fails if you changed an ORM model
     without adding a matching migration.  See
     [Changing the database schema](#changing-the-database-schema) below.
   - `Migration Safety` — **required**; seeds main's schema with synthetic
     data, runs your PR's migrations against it, and verifies the DB is
     still queryable.  See [docs/CI.md](docs/CI.md) for details.
   - `Smoke Test` — full-stack compose E2E; also runs nightly and on every
     main push as the pre-deploy gate.
4. Use `gh pr merge --auto --squash` (or the "Enable auto-merge" UI button).
   `Migration Safety` being required means auto-merge waits for real
   validation, so it's safe to enable on any PR.

## Changing the CI pipeline

**Read [docs/CI.md](docs/CI.md) first** — it documents the pipeline shape,
required checks, branch-protection config, the seed-script quirks, common
failure modes, and future cleanup items.

Key files if you need to touch CI:

- `.github/workflows/ci-gate.yml` — the required always-pass sentinel.
- `.github/workflows/tests.yml` — unit + e2e PR tests.
- `.github/workflows/alembic-check.yml` — ORM/migration drift gate.
- `.github/workflows/migration-safety.yml` — PR migration guard-rail.
- `.github/workflows/nightly.yml` — **display name `Smoke Test`**; runs
  nightly, on main push (pre-deploy gate), and on PRs.
- `.github/workflows/publish-image.yml` — build/push/deploy, triggered by a
  successful Smoke Test on main.  Also auto-bumps `__version__`.
- `scripts/ci_seed_synthetic.py` — synthetic data seeder.  **Has a quirks
  list at the top — read it before editing.**
- `scripts/ci_verify_post_migration.py` — post-migration verifier.

### If you rename a workflow

`publish-image.yml`'s `workflow_run` trigger keys off the workflow
**display name** (not the filename).  If you change `name:` on
`nightly.yml`, update `workflows: [...]` in `publish-image.yml` in the
same PR.

### If you add a new required check

Update branch protection on `main` to include the new check context:

```powershell
'{"strict":false,"contexts":["ci-gate","migration-safety","<new-check>"]}' |
  gh api -X PATCH repos/sslivins/agora-cms/branches/main/protection/required_status_checks --input -
```

## Coding conventions

Match the style of surrounding code.  Run the existing linters/tests before
pushing; do not add new tooling without discussion.
