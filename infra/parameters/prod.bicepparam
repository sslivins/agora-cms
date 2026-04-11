using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// prod.bicepparam — Production parameter values
//
// Fill in the secure values before deploying:
//   az deployment group create \
//     --resource-group agora-cms-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/prod.bicepparam \
//     --parameters postgresAdminPassword='<secure>' \
//                  cmsSecretKey='<secure>' \
//                  cmsAdminPassword='<secure>'
// ──────────────────────────────────────────────────────────────

param prefix = 'agora'
param location = 'westus2'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Secure params — pass via CLI or Key Vault reference, never commit values:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'

// Your Azure AD object ID (run: az ad signed-in-user show --query id -o tsv)
param adminPrincipalId = '<your-azure-ad-object-id>'

// Container images — leave empty to use ACR defaults, or override:
// param cmsImage = 'agoraacr.azurecr.io/agora-cms:latest'
// param mcpImage = 'agoraacr.azurecr.io/agora-cms-mcp:latest'
