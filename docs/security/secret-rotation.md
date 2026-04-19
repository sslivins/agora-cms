# Secret rotation runbook

This is the canonical procedure for rotating every credential-shaped
value used by an Agora CMS deployment. Keep this up to date whenever a
new credential is introduced — the goal is that any operator (or the
next on-call engineer) can rotate without archaeology.

## Inventory

| Secret | Where it lives | Rotation cadence | Blast radius if leaked |
|---|---|---|---|
| `AGORA_CMS_SECRET_KEY` | Compose `.env` / Azure Key Vault `cms-secret-key` | On every operator handover; on suspected compromise | All active session cookies + CSRF tokens forgeable |
| `AGORA_CMS_ADMIN_PASSWORD` | Compose `.env` / Azure Bicep `cmsAdminPassword` | First boot only; after that, change via UI | Full admin takeover |
| `POSTGRES_PASSWORD` | Compose `.env` / Azure PG managed identity | On provider-driven schedule (or on compromise) | DB read/write from VNet |
| Device API keys | Per-device, stored hashed in `devices.device_api_key_hash` | Automatic every `AGORA_CMS_API_KEY_ROTATION_HOURS` (default 24h) | Single-device impersonation |
| MCP service key | Shared volume `/shared/mcp-service.key` **or** Key Vault `mcp-service-key` | On MCP container rebuild; on suspected compromise | MCP tools can read/mutate CMS as service principal |
| Operator-created API keys | `api_keys` table, hashed | On operator offboarding; on suspected compromise | Scoped to operator's RBAC permissions |
| `AZURE_STORAGE_ACCOUNT_KEY` (Azure only) | Key Vault + env | Azure-provider cadence | Storage account read/write |
| `VERSION_BUMP_TOKEN` (CI) | Repo secrets | On maintainer change | Can push to `main` as the PAT owner |
| `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` (CI) | Repo secrets | On service-principal change | Deploy/update prod Azure resources |

## Procedure: `AGORA_CMS_SECRET_KEY`

Rotating the signing key invalidates every active session. Expect all
logged-in users to be kicked out once the new key is live.

1. Generate a new value:
   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
2. Compose deployments: update `.env`, then `docker compose up -d cms`.
3. Azure deployments: update Key Vault secret `cms-secret-key`, then
   redeploy the container app (or bump its revision).
4. Verify: look for `default-secret:` lines in the first 30s of startup
   logs — there should be **no** warning. Users logging back in should
   receive fresh cookies.

## Procedure: `AGORA_CMS_ADMIN_PASSWORD`

Preferred: change via the UI (`Settings → Account`). Env-var rotation
is only for lost-admin recovery.

1. Generate a new value; write it to a secrets manager.
2. Update `.env` / Key Vault.
3. Set `AGORA_CMS_RESET_PASSWORD=true` **once**.
4. Restart the CMS container. On boot the admin password is rewritten
   from env.
5. **Set `AGORA_CMS_RESET_PASSWORD=false` and restart** — otherwise
   every subsequent restart re-applies the env password.

## Procedure: device API keys

Automatic. Each device's WebSocket session negotiates a fresh key on a
rolling window controlled by `AGORA_CMS_API_KEY_ROTATION_HOURS`. To
force an early rotation for a suspected-compromised device:

1. In the UI, go to `Devices → <device> → Actions → Factory Reset` and
   check "re-adopt on next connect" — the old key is invalidated.
2. Confirm the device reappears in `Pending Devices` and re-adopt.
3. Old `previous_api_key_hash` is kept for a short grace window and
   then discarded.

## Procedure: operator-created API keys

1. `Settings → API Keys`, find the key, click **Revoke**. The hash is
   immediately deleted — in-flight requests with that key start
   returning 401.
2. If the key appeared in logs or was emailed, also rotate any
   downstream values that were derived from it.

## Procedure: MCP service key

1. Compose: stop both CMS and MCP. Delete the shared volume file
   `/shared/mcp-service.key`. Start CMS first — it regenerates the key.
   Start MCP — it reads the new value.
2. Azure: in Key Vault, create a new version of `mcp-service-key`. Both
   container apps pick it up on next restart. Confirm MCP tools still
   authenticate (e.g. hit `/api/mcp/auth`).

## Procedure: GitHub repo secrets

Settings → Secrets and variables → Actions. For each rotated secret:

1. Update the value in GitHub.
2. Re-run the latest `main` deploy workflow (publish-image) to confirm
   the new credential works end-to-end.
3. Revoke the old credential at the source (Azure AD app secret, PAT,
   etc.) — **after** confirming the new one works.

## Post-incident checklist

If you're here because a secret was exposed (public commit, shared
log, stolen laptop):

- [ ] Rotate the specific secret per its procedure above.
- [ ] If the secret was ever in git history, assume it is compromised
  forever — rotation is mandatory even if the commit was force-pushed.
- [ ] Search logs for the 24h window before and after the exposure for
  unexpected usage of the credential.
- [ ] Add a `.gitleaks.toml` rule so the Secrets Scan workflow catches
  any future reappearance.
- [ ] File an incident note under `docs/security/incidents/` with a
  timeline and the rotation timestamp.

## Related

- `cms/security.py` — startup-time default-credential detector. Grep for
  `default-secret:` in boot logs.
- `.github/workflows/secrets-scan.yml` — weekly + PR gitleaks sweep.
- `.gitleaks.toml` — repo-specific allowlist.
- Issue [#308](https://github.com/sslivins/agora-cms/issues/308) —
  tracking issue this runbook satisfies.
