# Assistant prod-rollout runbook

This is the canonical procedure for turning the Assistant feature on in
a fresh production environment (e.g. Goodwill prod). It is the prod
counterpart to the dev rollout that happened on 2026-05-29 → 2026-05-31;
the recurring failure modes from that rollout are captured in
`Common gotchas`, below.

Pre-reqs: you have `az` logged in to the target tenant, and you have
**Owner** or **User Access Administrator** on the target resource
group. (The deploy SP only has `Contributor`; the role-assignment
steps below cannot be run from CI until that gap is closed — see
`Follow-ups`.)

## Inventory

| Resource | Purpose | Created by |
|---|---|---|
| `Microsoft.CognitiveServices/accounts` (AOAI) | Hosts the GPT-4o `chat` deployment the agent loop calls | `infra/main.bicep` when `deployAzureOpenAI=true` |
| GPT-4o model deployment named `chat` | The deployment name `AZURE_OPENAI_DEPLOYMENT` resolves to | Bicep |
| Managed identity on `agoracms-cms` container app | Auth for both Key Vault and AOAI; no API keys anywhere | Container app default |
| Managed identity on `agora-cms-mcp` container app | Auth for Key Vault (to read the MCP service key) | Container app default |
| KV secret `mcp-service-key` | Shared service token CMS uses to call MCP on behalf of users | Seeded by MCP at startup |
| CMS setting `assistant_enabled_user_ids` | JSON array of user UUIDs; only these users see / hit the feature | Admin via Settings UI |
| CMS setting `assistant_monthly_budget_usd` | Per-user $ cap; HTTP 429 once exceeded | Admin via Settings UI |

## Procedure

### 1. Provision AOAI

Run the bicep deploy with `deployAzureOpenAI=true` against the prod
resource group. Pre-confirm GPT-4o quota in the chosen region
(westus3 is the default — match dev unless there's a quota reason to
move).

```sh
az deployment group create \
  --resource-group <prod-rg> \
  --template-file infra/main.bicep \
  --parameters @infra/main.prod.bicepparam \
  --parameters deployAzureOpenAI=true
```

If the deploy fails on quota, file a quota request via the portal
(*Subscriptions → Usage + quotas → Request increase → Azure OpenAI*)
for the specific model + region. Quota approval is usually <24 h for
GPT-4o in established regions.

### 2. Grant the three RBAC roles

The deploy SP only has `Contributor` on the RG, so `deployRoleAssignments=true`
will silently no-op the role assignments. Grant them manually until
the SP gets `User Access Administrator` (see `Follow-ups`).

```sh
# CMS MI gets write access to KV so the budget service can persist usage state
az role assignment create \
  --assignee <cms-mi-principal-id> \
  --role "Key Vault Secrets Officer" \
  --scope /subscriptions/<sub>/resourceGroups/<prod-rg>/providers/Microsoft.KeyVault/vaults/<prod-kv>

# MCP MI needs to READ the shared mcp-service-key secret
az role assignment create \
  --assignee <mcp-mi-principal-id> \
  --role "Key Vault Secrets User" \
  --scope /subscriptions/<sub>/resourceGroups/<prod-rg>/providers/Microsoft.KeyVault/vaults/<prod-kv>

# CMS MI calls AOAI; this role grants chat-completions access (no Cognitive Services Contributor needed)
az role assignment create \
  --assignee <cms-mi-principal-id> \
  --role "Cognitive Services OpenAI User" \
  --scope /subscriptions/<sub>/resourceGroups/<prod-rg>/providers/Microsoft.CognitiveServices/accounts/<prod-aoai>
```

Find the principal IDs with:

```sh
az containerapp show -g <prod-rg> -n agoracms-cms      --query identity.principalId -o tsv
az containerapp show -g <prod-rg> -n agora-cms-mcp     --query identity.principalId -o tsv
```

### 3. Seed `mcp-service-key`

The MCP container creates this KV secret on its first startup IF its
MI can write to KV. To trigger:

1. Open the CMS Settings page → MCP tab.
2. Toggle MCP **off**, save.
3. Toggle MCP **on** again, save.

This restarts the MCP container, which re-reads its config, sees the
key is missing, and creates it. Confirm with:

```sh
az keyvault secret show --vault-name <prod-kv> --name mcp-service-key --query attributes.created -o tsv
```

### 4. Burn the AOAI RBAC propagation window

Azure RBAC takes **5–60 minutes** to propagate to Cognitive Services
data-plane requests. Until it does, the agent returns a confusing
cascade — 401 with one error body, 401 with a different body, then
finally 200. Users hitting this mid-rollout will rage-quit.

Mitigation: from your own (admin / always-enabled) account, send 2–3
dummy prompts ("hi") through the Assistant after step 2 and BEFORE
flipping the allowlist in step 5. Each prompt either fails fast (RBAC
not yet ready — wait a minute, retry) or succeeds. Once you get two
consecutive successes, the cascade is burned in and real users won't
see it.

### 5. Flip the allowlist

Settings → Assistant card → paste the UUIDs of the users you want to
enable. Start with one ("stesli@" or the Goodwill prod equivalent),
verify end-to-end, then expand.

```sql
-- Look up a user UUID:
SELECT id, username, email FROM users WHERE email = 'stesli@example.com';
```

The allowlist is a JSON array stored in `cms_settings` under key
`assistant_enabled_user_ids`. The admin user (anyone with
`settings:write`) is always enabled as a permanent escape hatch — you
do not need to add yourself.

### 6. Set the monthly budget cap

Settings → Assistant card → **Monthly budget (USD)**. Defaults to $5
per user per month if unset.

* `cap > 0` — normal enforcement; HTTP 429 once exceeded
* `cap = 0` — feature paused (sends respond with budget-exceeded error)
* `cap < 0` — unlimited (for emergency / debug; record why in audit log)

### 7. Verify

From an allowlisted account:

1. Open `/assistant` — sidebar + composer + new-thread button should render.
2. Send "list the devices in this deployment" — should stream tokens,
   show a `Calling list_devices…` line, and produce a numbered list.
3. Send "create a tag called rollout-smoke" — should stream, show the
   inline Approve / Reject card with the literal args, and NOT execute
   until you click Approve.
4. Click Approve — tag should be created (verify in Devices → Tags).
5. Send "what's my monthly usage" — should respond with $0.0x figure
   that matches `assistant_budget_summary` in the Settings UI.

## Common gotchas

These all bit the dev rollout — capturing here so they don't bite
prod too:

* **The 401 cascade (step 4)** — biggest single source of "the
  assistant is broken" reports during dev rollout. Always burn it
  in before flipping the allowlist.
* **Stale system prompt** — fixed in PRs #667 + #672 and now locked
  in by `tests/test_chat_smoke.py::TestSystemPromptContract`. If you
  ever see the LLM say "I don't have tools" or "I'd recommend checking
  the UI", check `cms/services/assistant/prompts.py` first.
* **Write tools missing from the catalog** — fixed in PR #670 and
  locked in by `tests/test_chat_smoke.py::TestMcpToolExposure`. If
  CRUD requests get refused with "I can't do that", check that
  `AssistantMcpClient.list_openai_tools` is filtering by
  `ALLOWED_TOOLS` not `READ_ONLY_TOOLS`.
* **`openapi-check` CI gate** — any PR that touches a route or schema
  must include a regenerated `docs/openapi.yaml`. Run
  `python scripts/generate_openapi.py` before pushing.
* **CRLF noise on `docs/openapi.yaml`** — Windows checkouts will
  show the file as "modified" right after `generate_openapi.py` even
  when the route surface didn't change. Run `git diff` — if the
  content diff is empty, just `git checkout -- docs/openapi.yaml`
  and skip the file in the commit.
* **Approval card shows args the user didn't ask for** — fixed by
  #672's "never invent params" prompt language. If you see this come
  back, the prompt regressed.

## Rollback

If the rollout goes sideways:

1. Settings → Assistant card → clear `assistant_enabled_user_ids`
   (set to `[]`). Feature is now off for everyone except the admin
   escape hatch.
2. If the issue is AOAI-side (the deployment is throwing 5xx),
   `assistant_monthly_budget_usd = 0` also pauses the feature
   immediately.
3. The AOAI account itself can be left provisioned — there's no cost
   when nobody's calling it. Removing it requires re-running the RBAC
   grants on the next provision.

## Follow-ups

* **Grant the deploy SP `User Access Administrator` on the RG.** Once
  this lands, steps 2 of this runbook collapses into the bicep deploy
  via `deployRoleAssignments=true`. The same gap exists in every
  Agora env build; fixing it once at the org level eliminates every
  manual-RBAC step on future env builds.
* **Automate step 4 (RBAC burn-in)** — a 60-second post-deploy job
  that hammers AOAI from the CMS MI until two consecutive 2xx, then
  exits. Removes the human-burn-in step entirely.

