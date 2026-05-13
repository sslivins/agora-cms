using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// prod.bicepparam — Production parameter values
//
// Deployed automatically by the CD pipeline on every merge to main.
// Secure values are passed from GitHub Secrets — never commit them here.
//
// Manual deploy:
//   az deployment group create \
//     --resource-group agoracms-cms-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/prod.bicepparam \
//     --parameters postgresAdminPassword='<secure>' \
//                  cmsSecretKey='<secure>' \
//                  cmsAdminPassword='<secure>'
// ──────────────────────────────────────────────────────────────

param prefix = 'agoracms'
param location = 'westus3'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Secure params — passed via CLI or GitHub Secrets, never commit values:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'
// param adminPrincipalId    = '<set-via-cli>'   # the CD pipeline supplies this
//                                                # from secrets.ADMIN_PRINCIPAL_ID.
//                                                # Do NOT hardcode an Entra OID
//                                                # here — a stale value would
//                                                # silently roll the wrong tenant
//                                                # on a manual `az deployment
//                                                # group create` without the
//                                                # workflow's override.

// Telemetry alert recipient(Phase 0 / A1.5 — issue #474) is supplied by the
// CD pipeline from the GitHub repo variable ALERT_EMAIL. Leave the default
// empty here so we don't commit a routable address.

// Container images — set by CD pipeline via --parameters override:
// param cmsImage = 'ghcr.io/sslivins/agora-cms@<digest>'
// param mcpImage = 'ghcr.io/sslivins/agora-cms-mcp@<digest>'
// param workerImage = 'ghcr.io/sslivins/agora-worker@<digest>'
