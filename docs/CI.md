# CI / CD Pipeline

This document describes how code gets from a PR to production in `agora-cms`,
which checks are required to merge, and the quirks in the pipeline that future
maintainers (human or AI) need to know about to avoid regressions.

## End-to-end flow

```
PR opened / updated
  ├─ CI Gate          (always-pass sentinel — REQUIRED)
  ├─ Tests/test       (pytest suite)
  ├─ Tests/e2e        (compose-based e2e)
  ├─ Alembic Check    (REQUIRED — model/migration drift)
  ├─ Migration Safety (REQUIRED — see below)
  └─ Smoke Test       (full-stack compose E2E)

Merge to main (squash)
  └─ Smoke Test (on main push)        .github/workflows/nightly.yml
       └─ Publish & Deploy             .github/workflows/publish-image.yml
            ├─ build + push image
            ├─ deploy
            ├─ post-deploy /api/system/health probe
            └─ auto-bump cms/__init__.py __version__ (commits back to main)
```

Smoke Test also runs on a nightly cron.

## Required status checks on `main`

- `ci-gate` — always-pass sentinel (`.github/workflows/ci-gate.yml`). It
  exists so branch protection has *something* mandatory to key off when the
  real checks are path-filtered / conditional.
- `alembic-check` — ORM/migration drift guard (see below).
- `migration-safety` — the real PR gate (see below).

Branch protection is classic (not rulesets) with `enforce_admins: false` and
`strict: false` (no up-to-date-before-merge requirement, so PRs don't have to
rebase every time main moves).

Smoke Test is intentionally **not** a required check — it's too slow for a
per-PR block, and nightly + post-merge coverage is sufficient.

## Alembic Check (`.github/workflows/alembic-check.yml`)

Catches the class of bug that took out production in PR #280 (column added
to an ORM model with no matching DDL).

Flow:

1. Spin up a fresh Postgres service.
2. `alembic upgrade head` — build the schema from migrations.
3. `alembic check` — compare ORM metadata (`target_metadata` in
   `alembic/env.py`) to the live schema.  If autogenerate would produce
   any new operations, the check exits non-zero.

The check is fast (seconds) and runs on every PR regardless of path.  To
fix a failure, add a migration:

```bash
AGORA_CMS_DATABASE_URL=postgresql+asyncpg://agora:agora@localhost:5432/agora_cms \
  alembic revision --autogenerate -m "describe your change"
```

then commit the generated file in `alembic/versions/`.

Forward-only by policy: `alembic/script.py.mako`'s `downgrade()` raises
`NotImplementedError`, so don't bother filling it in.  Never edit a
merged migration — add a new one instead.

## Migration Safety (`.github/workflows/migration-safety.yml`)

The critical guard-rail that stops a PR from silently breaking migrations
against a populated database.

Flow:

1. Check out the PR (`pr/`) and `main` (`base/`) in parallel directories.
2. Copy `pr/scripts/ci_seed_synthetic.py` → `base/scripts/` so seeding always
   uses the latest seed logic even when testing against main's code.
3. Install `base/` deps. Initialise schema from main (by calling
   `run_migrations()`, which now runs `alembic upgrade head` for fresh DBs
   or `alembic stamp head` for legacy pre-Alembic schemas). Run
   `scripts/ci_seed_synthetic.py` with `PYTHONPATH=base/` — this seeds
   synthetic data into a DB with main's schema.
4. Install `pr/` deps. Run `run_migrations()` on the PR branch against the
   populated DB.  Alembic picks up from main's head revision and applies
   any migrations the PR added.
5. Run `scripts/ci_verify_post_migration.py` with `PYTHONPATH=pr/` — asserts
   that the populated DB is still queryable and the required tables are
   non-empty.

If step 4 or 5 fails, the PR cannot merge.

`alembic-check` catches *missing* migrations; `migration-safety` catches
*broken* migrations (destructive, non-idempotent, or incompatible with
existing data).  They complement each other.

### Quirks / don't-regress list

These have all bitten us. They are documented in `scripts/ci_seed_synthetic.py`
as well, but listed here for cross-referenceability.

- **`assets.asset_type`** is an enum at the DB level. Caller must supply
  `asset_type="VIDEO"` (or another valid enum value), **not** `type="VIDEO"`.
  The auto-placeholder generator would produce `"seed_asset_type_NN"` which is
  not a valid enum value.
- **`asset_variants`** FK column is `source_asset_id`, **not** `asset_id`.
- **`devices.id`** is `VARCHAR(64)`, not `UUID`. Must pass
  `str(uuid.uuid4())`, not a `UUID` object — asyncpg refuses to coerce.
- **`api_keys.key_hash`** is unique and `VARCHAR(64)`. The seed value must be
  structured so that truncation at 64 chars does not collapse rows to
  identical values — put the differentiator *at the start*, not the end.

## Nightly / Smoke Test (`.github/workflows/nightly.yml`)

Display name: **Smoke Test**. File is still called `nightly.yml` and tests
live in `tests/nightly/` — these paths are kept for history/URL stability.

Triggered by:
- `schedule:` — nightly cron.
- `push: branches: [main]` — every main merge, as the pre-deploy gate.
- Path-filtered PR trigger.

The `workflow_run` trigger in `publish-image.yml` is keyed off the workflow's
**display name** (`Smoke Test`), not its filename. If you ever rename it
again, update both files together.

## Publish & Deploy (`.github/workflows/publish-image.yml`)

- Triggered by a successful Smoke Test run on main (`workflow_run` →
  `workflows: [Smoke Test]`).
- Builds + pushes the image, deploys, then hits
  `/api/system/health` as a post-deploy smoke probe.
- Finally, auto-bumps `cms/__init__.py`'s `__version__` and commits the bump
  back to `main` using a PAT stored in `VERSION_BUMP_TOKEN` (repo secret).

### `VERSION_BUMP_TOKEN`

- Personal Access Token (classic, not fine-grained) with `contents: write`
  on this repo.
- Currently has a **90-day expiry**. Rotate before expiry or deploys will
  stop bumping the version.
- The bot identity is whatever user owns the PAT — bypasses branch
  protection because admins are not enforced (`enforce_admins: false`).

## Seed script shape (`scripts/ci_seed_synthetic.py`)

Key helpers:

- `_table_info(conn, table)` — introspects columns: name, nullable, default,
  data_type, max_len.
- `_fk_targets(conn, table)` — introspects FK constraints so we can skip
  rows whose FK targets don't yet exist.
- `_any_existing_pk(conn, table)` — picks an arbitrary existing PK for an
  FK target.
- `_placeholder_for(col)` — generates a placeholder value based on type:
  - `ARRAY` → `[]`
  - `JSON`/`JSONB` → `{}`
  - `UUID` → `uuid.uuid4()`
  - `VARCHAR(n)` → string truncated to `n`
  - ints, bools, datetimes handled appropriately
- `_insert(conn, table, values)` — caller-supplied string values are also
  truncated to the column's max_len. Each insert runs inside a savepoint
  (`conn.begin_nested()`) so a bad row doesn't poison subsequent inserts.

Seeding order is FK-aware:

```
roles → users → device_groups → device_profiles → devices
     → assets → asset_variants → schedules → api_keys
```

## Verify script (`scripts/ci_verify_post_migration.py`)

- `REQUIRED_NONEMPTY = {"users", "devices", "assets"}` — if any of these is
  empty after migrations, the job fails. Other tables may legitimately be
  empty (e.g. if their FK chain broke during seed).
- Runs sanity join queries:
  - `devices ⋈ device_groups ⋈ device_profiles`
  - `assets ⋈ asset_variants ON v.source_asset_id = a.id`

## Auto-merge etiquette

- Use `gh pr merge --auto --squash` (or equivalent in the UI) once the PR
  looks ready. Because `migration-safety` is now required, auto-merge will
  actually wait for real validation before merging.
- Historical trap (now fixed): prior to migration-safety being required,
  auto-merge would fire the moment `ci-gate` was green, merging PRs whose
  other checks were still failing/pending. If you ever remove
  migration-safety from the required list, remember this.

## Common failure modes & fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `invalid input value for enum assettype: "seed_asset_type_NN"` | seed caller passing wrong key (e.g. `type` instead of `asset_type`) | pass `asset_type="VIDEO"` |
| `invalid input for query argument $1: UUID(...) (expected str, got UUID)` | `devices.id` is VARCHAR, not UUID | `str(uuid.uuid4())` |
| `duplicate key value violates unique constraint ... key_hash ...` | truncation collapsing unique suffix | put differentiator at start of string |
| `column v.asset_id does not exist` | wrong FK column name on `asset_variants` | use `source_asset_id` |
| `TypeError: init_db() missing 1 required positional argument: 'settings'` | calling `await init_db()` | it's sync; `init_db(get_settings())` then `await wait_for_db()` |

## Future improvements

- Enum-aware placeholder generation: query `pg_enum` instead of requiring
  callers to hard-code enum values.
- Move the inline `python - <<'PY'` block in `migration-safety.yml` to a
  proper script (e.g. `scripts/ci_run_migrations.py`) for testability.
- Rename file/dirs: `nightly.yml` → `smoke.yml`, `tests/nightly/` →
  `tests/smoke/`. Kept out of the initial rename to minimise diff.
