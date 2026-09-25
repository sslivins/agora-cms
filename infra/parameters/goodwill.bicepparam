using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// goodwill.bicepparam — Goodwill tenant parameter values
//
// THIS FILE IS APPLIED BY THE DEPLOY. It is passed to
// `az deployment group create --parameters` by
// .github/workflows/deploy-goodwill.yml (manual workflow_dispatch).
//
// It was previously documentation only: the workflow passed every
// value inline and never referenced this file, so edits here had no
// effect.  That drift is what left Voice Announcements unprovisioned
// in prod — `deployAzureSpeech` was absent from the inline list, so
// main.bicep's default of false silently applied.
//
// Rule of thumb: anything describing THE ENVIRONMENT belongs here.
// Anything describing A SINGLE RUN (image tags, revision suffixes)
// stays as an inline override in the workflow.
//
// Manual deploy (rare — prefer the workflow); secrets come from the
// environment, matching how CI supplies them:
//   $env:POSTGRES_ADMIN_PASSWORD = '<secure>'   # + CMS_SECRET_KEY,
//   $env:CMS_ADMIN_PASSWORD = '<secure>'        #   ADMIN_PRINCIPAL_ID
//   az deployment group create \
//     --resource-group agoragw-cms-rg \
//     --parameters infra/parameters/goodwill.bicepparam \
//     --parameters cmsImage='...' mcpImage='...' workerImage='...'
//
// Sizing: prod-equivalent (no cpu/memory overrides — main.bicep
// defaults apply: cms=1.0/2Gi, mcp=0.5/1Gi, worker=4.0/8Gi).
//
// Region: westus2 (Quincy, WA) — geographically closest to Seattle
// and broadest service coverage on the West Coast.  If Goodwill's
// subscription is quota-restricted in westus2, swap to westus3
// (Phoenix) — prod's region — and redeploy.
//
// adminPrincipalId is still never a literal here — it is read from
// the environment (ADMIN_PRINCIPAL_ID), which the workflow supplies
// from the goodwill environment's secret.  Keeping it out of the
// file prevents a stale value from a different tenant landing on a
// manual deploy.
// ──────────────────────────────────────────────────────────────

// INFRA_PREFIX is a GitHub Actions variable on the `seattle-goodwill`
// environment; the literal is the fallback for manual deploys.
param prefix = readEnvironmentVariable('INFRA_PREFIX', 'agoragw')
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

// ── Voice Announcements backend (Azure AI Speech) ──
// Prod opts in here.  westus (not the RG's westus2) for the same
// reason as AOAI above: voice/model availability is broader there,
// and it keeps dev + prod Speech in one region for quota tracking.
// Enabling this creates a billable S0 Speech account on the next
// deploy, plus two 'Cognitive Services Speech User' role assignments
// (CMS + worker managed identities) which require the deploy to run
// with deployRoleAssignments=true and an identity holding Owner/UAA.
param deployAzureSpeech = true
param azureSpeechRegion = 'westus'
param azureSpeechSku = 'S0'

// ── Values supplied by the deploy workflow's environment ──
//
// These are read from environment variables rather than committed.
// readEnvironmentVariable() is what makes this file usable as a real
// `--parameters` argument: a .bicepparam must assign every parameter
// that has no default in main.bicep (prefix + the four below), or the
// build fails BCP258.  That failure is why this file was previously
// bypassed entirely and every value passed inline instead.
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param cmsSecretKey = readEnvironmentVariable('CMS_SECRET_KEY')
param cmsAdminPassword = readEnvironmentVariable('CMS_ADMIN_PASSWORD')
param adminPrincipalId = readEnvironmentVariable('ADMIN_PRINCIPAL_ID', '')
param githubIssuesToken = readEnvironmentVariable('GITHUB_ISSUES_TOKEN', '')

// Environment-shaped, but stored as GitHub Actions variables so they
// can be changed without a code change.
param alertEmail = readEnvironmentVariable('ALERT_EMAIL', '')
param cmsBaseUrlOverride = readEnvironmentVariable('CMS_BASE_URL_OVERRIDE', '')

// Container images and revision suffixes are per-run values, not
// environment config, so the workflow still passes them as inline
// `--parameters` overrides (which take precedence over this file):
//   cmsImage / mcpImage / workerImage
//   cmsRevisionSuffix / previousCmsRevisionName
//   mcpRevisionSuffix / previousMcpRevisionName
//   deployRoleAssignments
