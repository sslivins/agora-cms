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
// param adminPrincipalId    = '<set-via-cli>'   # do NOT hardcode an Entra OID
//                                                # here — a stale value would
//                                                # silently roll the wrong tenant
//                                                # on a manual deploy.

// Telemetry alert recipient(Phase 0 / A1.5 — issue #474) is supplied by the
// CD pipeline from the GitHub repo variable ALERT_EMAIL. Leave the default
// empty here so we don't commit a routable address.

// Use smaller resources for dev
param cmsCpu = '0.5'
param cmsMemory = '1Gi'
param workerCpu = '1.0'
param workerMemory = '2Gi'
