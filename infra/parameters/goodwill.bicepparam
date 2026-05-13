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
