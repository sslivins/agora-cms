using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// goodwill.bicepparam — Goodwill tenant parameter values
//
// Deployed by .github/workflows/deploy-goodwill.yml (manual
// workflow_dispatch only — gated by the "goodwill" GH environment's
// required-reviewer rule).  Secure values are passed from the
// environment's GitHub Secrets — never commit them here.
//
// Manual deploy (rare — prefer the workflow):
//   az deployment group create \
//     --resource-group agoragw-cms-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/goodwill.bicepparam \
//     --parameters postgresAdminPassword='<secure>' \
//                  cmsSecretKey='<secure>' \
//                  cmsAdminPassword='<secure>' \
//                  adminPrincipalId='<entra-oid-in-goodwill-tenant>'
//
// Sizing: prod-equivalent (no cpu/memory overrides — main.bicep
// defaults apply: cms=1.0/2Gi, mcp=0.5/1Gi, worker=4.0/8Gi).
//
// Region: westus2 (Quincy, WA) — geographically closest to Seattle
// and broadest service coverage on the West Coast.  If Goodwill's
// subscription is quota-restricted in westus2, swap to westus3
// (Phoenix) — prod's region — and redeploy.
//
// adminPrincipalId is intentionally NOT set here.  It must be
// supplied at deploy time (the workflow passes it from the
// goodwill environment's ADMIN_PRINCIPAL_ID secret).  This prevents
// a stale value from a different tenant accidentally landing on a
// manual deploy.
// ──────────────────────────────────────────────────────────────

param prefix = 'agoragw'
param location = 'westus2'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Opt this environment into the Assistant feature backend.
// Phase 1 (dev pilot) validated the budget caps + approval UX on
// agoragwdev between 2026-05-29 and 2026-05-31; prod opts in here.
//
// AOAI account is pinned to westus (NOT the prod RG region of westus2)
// because westus2 has zero standard gpt-4o TPM quota at the time of
// writing, while westus has 970 units of headroom on a 1000 limit
// (dev account uses 30). Cross-region AOAI is supported — the CMS
// container app calls AOAI by FQDN, not via VNet, so colocating dev
// + prod AOAI in westus simplifies quota tracking too.
param deployAzureOpenAI = true
param azureOpenAIRegion = 'westus'
param azureOpenAIChatModel = 'gpt-4o'
param azureOpenAIChatModelVersion = '2024-11-20'
param azureOpenAIChatCapacity = 30

// Secure params — passed via CLI or the deploy-goodwill workflow,
// never commit values:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'
// param adminPrincipalId = '<set-via-cli>'

// Container images — set by the deploy-goodwill workflow via
// --parameters overrides. Goodwill pins by tag, not digest, since it
// consumes published releases rather than building:
// param cmsImage = 'ghcr.io/sslivins/agora-cms:<version>'
// param mcpImage = 'ghcr.io/sslivins/agora-cms-mcp:<version>'
// param workerImage = 'ghcr.io/sslivins/agora-worker:<version>'
