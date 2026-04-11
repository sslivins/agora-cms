<#
.SYNOPSIS
    Deploy Agora CMS infrastructure to Azure.

.DESCRIPTION
    One-command deployment: creates the resource group, deploys all Bicep
    modules, pushes container images to ACR, and prints connection info.

    Prerequisites:
      - Azure CLI (az) installed and on PATH

.PARAMETER Subscription
    Azure subscription name or ID.

.PARAMETER Location
    Azure region. Defaults to westus2.

.PARAMETER Prefix
    Resource-name prefix (3-12 chars, lowercase). Defaults to "agora".

.PARAMETER ResourceGroup
    Resource-group name. Defaults to "<Prefix>-cms-rg".

.PARAMETER SkipImagePush
    Skip building/pushing container images to ACR.

.EXAMPLE
    .\infra\deploy.ps1 -Subscription "My Azure Sub"

.EXAMPLE
    .\infra\deploy.ps1 -Subscription "abc-123" -Location eastus -Prefix myagora
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, HelpMessage = "Azure subscription name or ID")]
    [string]$Subscription,

    [Parameter(HelpMessage = "Azure region")]
    [string]$Location = "westus2",

    [Parameter(HelpMessage = "Resource name prefix (3-12 chars, lowercase)")]
    [ValidatePattern("^[a-z][a-z0-9]{2,11}$")]
    [string]$Prefix = "agora",

    [Parameter(HelpMessage = "Resource group name")]
    [string]$ResourceGroup = "",

    [Parameter(HelpMessage = "PostgreSQL admin password")]
    [string]$PostgresPassword = "",

    [Parameter(HelpMessage = "CMS secret key for JWT/session signing")]
    [string]$CmsSecretKey = "",

    [Parameter(HelpMessage = "CMS web admin password")]
    [string]$CmsAdminPassword = "",

    [switch]$SkipImagePush
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ──────────────────────────────────────────────────────

function Write-Step  { param([string]$msg) Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$msg) Write-Host "  ✗ $msg" -ForegroundColor Red }

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

# ── Pre-flight checks ───────────────────────────────────────────

Write-Host ""
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Magenta
Write-Host "  Agora CMS — Azure Deployment" -ForegroundColor Magenta
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Magenta

Write-Step "Pre-flight checks"

if (-not (Test-Command "az")) {
    Write-Fail "Azure CLI (az) not found. Install: https://aka.ms/installazurecli"
    exit 1
}
Write-Ok "Azure CLI found"

# ── Authenticate & set subscription ──────────────────────────────

Write-Step "Setting Azure subscription"

# Check if already logged in
$account = az account show 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
if (-not $account) {
    Write-Host "  Launching browser login..." -ForegroundColor Yellow
    az login | Out-Null
}

az account set --subscription $Subscription 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Could not set subscription '$Subscription'"
    Write-Host "  Available subscriptions:" -ForegroundColor Yellow
    az account list --query "[].{Name:name, Id:id}" -o table
    exit 1
}

$subInfo = az account show --query "{name:name, id:id}" -o json | ConvertFrom-Json
Write-Ok "Subscription: $($subInfo.name) ($($subInfo.id))"

# ── Get admin principal ID ───────────────────────────────────────

Write-Step "Resolving your Azure AD identity"
$adminPrincipalId = (az ad signed-in-user show --query id -o tsv).Trim()
if (-not $adminPrincipalId) {
    Write-Fail "Could not resolve signed-in user. Run 'az login' first."
    exit 1
}
Write-Ok "Admin principal: $adminPrincipalId"

# ── Collect secrets ──────────────────────────────────────────────

Write-Step "Collecting secrets"

if ($PostgresPassword) {
    $pgPass = $PostgresPassword
} else {
    $sec = Read-Host -AsSecureString "  PostgreSQL admin password"
    $pgPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

if ($CmsSecretKey) {
    $cmsKey = $CmsSecretKey
} else {
    $sec = Read-Host -AsSecureString "  CMS secret key (for JWT/session signing)"
    $cmsKey = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

if ($CmsAdminPassword) {
    $cmsPass = $CmsAdminPassword
} else {
    $sec = Read-Host -AsSecureString "  CMS web admin password"
    $cmsPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

if ($pgPass.Length -lt 8) {
    Write-Fail "PostgreSQL password must be at least 8 characters."
    exit 1
}

Write-Ok "Secrets collected"

# ── Create resource group ────────────────────────────────────────

if (-not $ResourceGroup) { $ResourceGroup = "$Prefix-cms-rg" }

Write-Step "Creating resource group: $ResourceGroup ($Location)"
az group create --name $ResourceGroup --location $Location --tags project=agora-cms managedBy=bicep -o none
Write-Ok "Resource group ready"

# ── Recover soft-deleted Key Vault if needed ─────────────────────

$kvName = "$Prefix-kv"
Write-Step "Checking for soft-deleted Key Vault ($kvName)"
$deletedKv = az keyvault list-deleted --query "[?name=='$kvName'].name" -o tsv 2>$null
if ($deletedKv) {
    Write-Warn "Found soft-deleted Key Vault '$kvName' — recovering"
    az keyvault recover --name $kvName -o none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Key Vault recovered"
        # Move it to our resource group if it was in a different one
        # (recovery restores to original location — Bicep will update it)
    } else {
        Write-Fail "Could not recover Key Vault. You may need to use a different prefix."
        exit 1
    }
} else {
    Write-Ok "No soft-deleted Key Vault — clean deploy"
}

# ── Resolve paths ────────────────────────────────────────────────

$scriptDir = $PSScriptRoot
$templateFile = Join-Path $scriptDir "main.bicep"

if (-not (Test-Path $templateFile)) {
    $templateFile = Join-Path (Get-Location) "infra\main.bicep"
}
if (-not (Test-Path $templateFile)) {
    Write-Fail "Cannot find main.bicep. Run from the repo root or infra/ directory."
    exit 1
}

$repoRoot = Split-Path $scriptDir -Parent
$acrName = ($Prefix -replace '-','') + "acr"

# ── Pre-create ACR & push images ─────────────────────────────────
# Container Apps need images to exist in ACR before Bicep can
# provision them, so we create ACR and push images first.

if (-not $SkipImagePush) {
    Write-Step "Creating container registry ($acrName)"
    az acr create --name $acrName --resource-group $ResourceGroup `
        --sku Basic --location $Location --admin-enabled true `
        --tags project=agora-cms managedBy=bicep -o none 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Could not create ACR '$acrName'"
        exit 1
    }
    Write-Ok "ACR ready"

    Write-Step "Building container images in ACR ($acrName)"

    Write-Host "  Building CMS image..." -ForegroundColor Yellow
    az acr build --registry $acrName --image agora-cms:latest `
        --file "$repoRoot\Dockerfile" $repoRoot --no-logs 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "CMS image build failed"
        exit 1
    }
    Write-Ok "CMS image built & pushed"

    Write-Host "  Building MCP image..." -ForegroundColor Yellow
    az acr build --registry $acrName --image agora-cms-mcp:latest `
        --file "$repoRoot\mcp\Dockerfile" "$repoRoot\mcp" --no-logs 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "MCP image build failed"
        exit 1
    }
    Write-Ok "MCP image built & pushed"
} else {
    Write-Warn "Skipping image build (-SkipImagePush)"
}

# ── Deploy Bicep templates ───────────────────────────────────────

Write-Step "Deploying infrastructure (this takes 5-10 minutes)..."

$deployResult = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file $templateFile `
    --parameters `
        prefix=$Prefix `
        location=$Location `
        postgresAdminPassword=$pgPass `
        cmsSecretKey=$cmsKey `
        cmsAdminPassword=$cmsPass `
        adminPrincipalId=$adminPrincipalId `
    --query "properties.outputs" `
    -o json 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Bicep deployment failed. Run with verbose output:"
    Write-Host "  az deployment group create --resource-group $ResourceGroup --template-file $templateFile ..." -ForegroundColor Red
    exit 1
}

# Filter out any non-JSON lines (warnings) before parsing
$jsonLines = ($deployResult | Where-Object { $_ -match '^\s*[\{\[\"]' -or $_ -match '^\s*\}' -or $_ -match '^\s*\]' -or $_ -match '^\s*"' }) -join "`n"
$outputs = $jsonLines | ConvertFrom-Json

$cmsUrl          = $outputs.cmsUrl.value
$mcpUrl          = $outputs.mcpUrl.value
$acrLoginServer  = $outputs.acrLoginServer.value
$pgFqdn          = $outputs.postgresServerFqdn.value
$kvUri           = $outputs.keyVaultUri.value
$storageName     = $outputs.storageAccountName.value

Write-Ok "Infrastructure deployed"

# ── Post-deploy: configure MCP API key ───────────────────────────

Write-Step "Configuring MCP API key"

$cmsAppName = "$Prefix-cms"
$mcpAppName = "$Prefix-mcp"
$apiKey = ""
$mcpSseKey = ""

# Wait for CMS to be healthy
Write-Host "  Waiting for CMS to start..." -ForegroundColor Yellow
$maxAttempts = 30
$cmsReady = $false
for ($i = 1; $i -le $maxAttempts; $i++) {
    try {
        $health = Invoke-WebRequest -Uri "https://$cmsUrl/login" -TimeoutSec 5 -ErrorAction SilentlyContinue -MaximumRedirection 0
        if ($health.StatusCode -eq 200) { $cmsReady = $true; break }
    } catch {}
    if ($i -eq $maxAttempts) {
        Write-Warn "CMS not responding after $maxAttempts attempts — MCP key setup skipped"
        Write-Host "  You can configure it manually later." -ForegroundColor Yellow
    }
    Start-Sleep 10
}

if ($cmsReady) {
    # Login and create API key
    try {
        # Login returns a 303 redirect on success; follow it to capture the session cookie
        $null = Invoke-WebRequest -Uri "https://$cmsUrl/login" -Method POST `
            -Body @{username='admin'; password=$cmsPass} `
            -SessionVariable cmsSession -ErrorAction Stop

        # Create a CMS API key for the MCP server
        $keyResp = Invoke-RestMethod -Uri "https://$cmsUrl/api/keys" -Method POST `
            -WebSession $cmsSession `
            -ContentType "application/json" `
            -Body '{"name":"mcp-server"}'
        $apiKey = $keyResp.key
        Write-Ok "CMS API key created"

        # Enable MCP in CMS settings
        $null = Invoke-RestMethod -Uri "https://$cmsUrl/api/mcp/toggle" -Method POST `
            -WebSession $cmsSession `
            -ContentType "application/json" `
            -Body '{"enabled":true}'
        Write-Ok "MCP server enabled in CMS settings"

        # Generate MCP SSE auth key (for external clients like Copilot CLI)
        $mcpKeyResp = Invoke-RestMethod -Uri "https://$cmsUrl/api/mcp/generate-key" -Method POST `
            -WebSession $cmsSession
        $mcpSseKey = $mcpKeyResp.key
        Write-Ok "MCP SSE auth key generated"

        # Store API key in Key Vault
        az keyvault secret set --vault-name "$Prefix-kv" --name "mcp-api-key" --value $apiKey -o none 2>$null
        Write-Ok "API key stored in Key Vault"

        # Update MCP container with the real API key and restart to pick it up
        az containerapp secret set --name $mcpAppName --resource-group $ResourceGroup `
            --secrets "mcp-api-key=$apiKey" -o none 2>$null
        $suffix = "mcp$(Get-Date -Format 'MMddHHmm')"
        az containerapp update --name $mcpAppName --resource-group $ResourceGroup `
            --revision-suffix $suffix -o none 2>$null
        # Secret changes require a restart to take effect
        $activeRevision = (az containerapp revision list --name $mcpAppName `
            --resource-group $ResourceGroup --query "[?properties.active].name" -o tsv 2>$null).Trim()
        az containerapp revision restart --name $mcpAppName --resource-group $ResourceGroup `
            --revision $activeRevision -o none 2>$null
        Write-Ok "MCP container updated with API key (restarted)"

    } catch {
        Write-Warn "Failed to configure MCP API key: $($_.Exception.Message)"
        Write-Host "  You can configure it manually via the CMS settings page." -ForegroundColor Yellow
        $apiKey = ""
        $mcpSseKey = ""
    }
}

# ── Print summary ────────────────────────────────────────────────

Write-Host ""
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Deployment Complete!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  CMS URL:          https://$cmsUrl" -ForegroundColor White
Write-Host "  MCP URL:          https://$mcpUrl" -ForegroundColor White
Write-Host "  ACR:              $acrLoginServer" -ForegroundColor White
Write-Host "  PostgreSQL:       $pgFqdn" -ForegroundColor White
Write-Host "  Key Vault:        $kvUri" -ForegroundColor White
Write-Host "  Storage Account:  $storageName" -ForegroundColor White
Write-Host "  Resource Group:   $ResourceGroup" -ForegroundColor White
if ($mcpSseKey) {
    Write-Host ""
    Write-Host "  MCP SSE Auth:     Bearer $mcpSseKey" -ForegroundColor White
    Write-Host "  MCP SSE URL:      https://$mcpUrl/sse" -ForegroundColor White
}
Write-Host ""

# ── Save outputs to file for reference ───────────────────────────

$outputFile = Join-Path $scriptDir "deployment-outputs.json"
$outputObj = @{
    timestamp          = (Get-Date -Format "o")
    subscription       = $subInfo.name
    resourceGroup      = $ResourceGroup
    location           = $Location
    prefix             = $Prefix
    cmsUrl             = "https://$cmsUrl"
    mcpUrl             = "https://$mcpUrl"
    mcpSseUrl          = "https://$mcpUrl/sse"
    mcpSseKey          = if ($mcpSseKey) { $mcpSseKey } else { "" }
    acrLoginServer     = $acrLoginServer
    postgresServerFqdn = $pgFqdn
    keyVaultUri        = $kvUri
    storageAccountName = $storageName
}
$outputObj | ConvertTo-Json -Depth 5 | Set-Content $outputFile -Encoding UTF8
Write-Ok "Outputs saved to $outputFile"

# ── Next steps ───────────────────────────────────────────────────

Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Yellow
Write-Host "    1. Open CMS: https://$cmsUrl" -ForegroundColor Gray
Write-Host "    2. Login with admin / <your password>" -ForegroundColor Gray
if ($mcpSseKey) {
    Write-Host "    3. Configure MCP in Copilot CLI using the SSE URL and key above" -ForegroundColor Gray
}
Write-Host ""
