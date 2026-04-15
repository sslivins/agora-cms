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

param adminPrincipalId = '224d9903-ad74-4629-982b-1db94580d901'

// Container images — set by CD pipeline via --parameters override:
// param cmsImage = 'agoracmsacr.azurecr.io/agora-cms:latest'
// param mcpImage = 'agoracmsacr.azurecr.io/agora-cms-mcp:latest'
// param workerImage = 'agoracmsacr.azurecr.io/agora-worker:latest'
