using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// goodwill-dev.bicepparam — Goodwill DEV environment parameter values
//
// Deployed by .github/workflows/deploy-goodwill-dev.yml on every
// successful "Publish & Deploy" run on main (auto-deploy).  Gated by
// the `seattle-goodwill-dev` GH environment.
//
// This is a sibling of goodwill.bicepparam (the Goodwill PROD env).
// It targets resource group `agoragw-cms-dev-rg` in the same Goodwill
// tenant/subscription.  Resource names are scoped with the
// `agoragwdev` prefix so they cannot collide with prod's `agoragw-*`.
//
// Sizing: dev-sized container apps (matches infra/parameters/dev.bicepparam):
//   cms    : 0.5 vCPU / 1Gi
//   mcp    : 0.25 vCPU / 0.5Gi (main.bicep default for mcp at this scale)
//   worker : 1.0 vCPU / 2Gi
//
// Region: westus — matches the pre-created agoragw-cms-dev-rg.
// (Goodwill prod uses westus2; dev is in westus for cost/region
// flexibility. westus has full Container Apps + Postgres Flexible
// + Web PubSub coverage so all bicep modules apply unchanged.)
//
// Manual deploy (rare — prefer the workflow):
//   az deployment group create \
//     --resource-group agoragw-cms-dev-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/goodwill-dev.bicepparam \
//     --parameters postgresAdminPassword='<secure>' \
//                  cmsSecretKey='<secure>' \
//                  cmsAdminPassword='<secure>' \
//                  adminPrincipalId='<entra-oid-in-goodwill-tenant>'
//
// adminPrincipalId is intentionally NOT set here.  It must be supplied
// at deploy time (the workflow passes it from the seattle-goodwill-dev
// environment's ADMIN_PRINCIPAL_ID secret).
// ──────────────────────────────────────────────────────────────

param prefix = 'agoragwdev'
param location = 'westus'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Smaller container sizing for dev (mirrors dev.bicepparam):
param cmsCpu = '0.5'
param cmsMemory = '1Gi'
param workerCpu = '1.0'
param workerMemory = '2Gi'

// Secure params — passed via the deploy-goodwill-dev workflow, never commit values:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'
// param adminPrincipalId = '<set-via-cli>'

// Container images — set by the deploy-goodwill-dev workflow via
// --parameters overrides. Dev pins by tag and tracks whatever was
// just published to main:
// param cmsImage = 'ghcr.io/sslivins/agora-cms:<version>'
// param mcpImage = 'ghcr.io/sslivins/agora-cms-mcp:<version>'
// param workerImage = 'ghcr.io/sslivins/agora-worker:<version>'
