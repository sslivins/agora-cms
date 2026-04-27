using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// dev.bicepparam — Dev environment parameter values
// ──────────────────────────────────────────────────────────────

param prefix = 'agoradev'
param location = 'westus3'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Secure params — pass via CLI:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'

param adminPrincipalId = '224d9903-ad74-4629-982b-1db94580d901'

// Telemetry alert recipient (Phase 0 / A1.5 — issue #474). Same address as
// prod for now; flip to a dev-only alias if dev starts paging too much.
param alertEmail = 'sslivins+agora_alerts@gmail.com'

// Use smaller resources for dev
param cmsCpu = '0.5'
param cmsMemory = '1Gi'
param workerCpu = '1.0'
param workerMemory = '2Gi'
