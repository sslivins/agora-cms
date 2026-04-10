<#
.SYNOPSIS
    Deploy Agora CMS infrastructure to Azure.

.DESCRIPTION
    One-command deployment: creates the resource group, deploys all Bicep
    modules, pushes container images to ACR, and prints connection info.

    Prerequisites:
      - Azure CLI (az) installed and on PATH
      - Docker Desktop running (for image push)

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

if (-not $SkipImagePush) {
    if (-not (Test-Command "docker")) {
        Write-Warn "Docker not found — will skip image push (use -SkipImagePush to suppress)"
        $SkipImagePush = $true
    } else {
        Write-Ok "Docker found"
    }
}

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

$postgresPassword = Read-Host -AsSecureString "  PostgreSQL admin password"
$cmsSecretKey     = Read-Host -AsSecureString "  CMS secret key (for JWT/session signing)"
$cmsAdminPassword = Read-Host -AsSecureString "  CMS web admin password"

# Convert SecureString to plain text for az cli
function ConvertFrom-SecureStringPlain {
    param([System.Security.SecureString]$Secure)
    [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure))
}

$pgPass  = ConvertFrom-SecureStringPlain $postgresPassword
$cmsKey  = ConvertFrom-SecureStringPlain $cmsSecretKey
$cmsPass = ConvertFrom-SecureStringPlain $cmsAdminPassword

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

# ── Deploy Bicep templates ───────────────────────────────────────

Write-Step "Deploying infrastructure (this takes 5-10 minutes)..."

$scriptDir = $PSScriptRoot
$templateFile = Join-Path $scriptDir "main.bicep"

if (-not (Test-Path $templateFile)) {
    # Fallback: user may be running from repo root
    $templateFile = Join-Path (Get-Location) "infra\main.bicep"
}
if (-not (Test-Path $templateFile)) {
    Write-Fail "Cannot find main.bicep. Run from the repo root or infra/ directory."
    exit 1
}

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
    -o json 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Bicep deployment failed:"
    Write-Host $deployResult -ForegroundColor Red
    exit 1
}

$outputs = $deployResult | ConvertFrom-Json

$cmsUrl          = $outputs.cmsUrl.value
$mcpUrl          = $outputs.mcpUrl.value
$acrLoginServer  = $outputs.acrLoginServer.value
$pgFqdn          = $outputs.postgresServerFqdn.value
$kvUri           = $outputs.keyVaultUri.value
$storageName     = $outputs.storageAccountName.value

Write-Ok "Infrastructure deployed"

# ── Push container images to ACR ─────────────────────────────────

if (-not $SkipImagePush) {
    Write-Step "Pushing container images to ACR ($acrLoginServer)"

    az acr login --name ($acrLoginServer -split '\.')[0] 2>&1 | Out-Null

    # Tag & push CMS image
    $cmsImageTag = "$acrLoginServer/agora-cms:latest"
    Write-Host "  Building & pushing CMS image..." -ForegroundColor Yellow
    docker tag agora-cms:latest $cmsImageTag 2>$null
    if ($LASTEXITCODE -ne 0) {
        # No local image — try building
        $repoRoot = Split-Path $scriptDir -Parent
        docker build -t $cmsImageTag -f "$repoRoot\Dockerfile" $repoRoot
    } else {
        # Just tag the existing image
        docker tag agora-cms:latest $cmsImageTag
    }
    docker push $cmsImageTag
    Write-Ok "CMS image pushed"

    # Tag & push MCP image
    $mcpImageTag = "$acrLoginServer/agora-cms-mcp:latest"
    Write-Host "  Building & pushing MCP image..." -ForegroundColor Yellow
    docker tag agora-cms-mcp:latest $mcpImageTag 2>$null
    if ($LASTEXITCODE -ne 0) {
        $repoRoot = Split-Path $scriptDir -Parent
        docker build -t $mcpImageTag -f "$repoRoot\mcp\Dockerfile" "$repoRoot\mcp"
    } else {
        docker tag agora-cms-mcp:latest $mcpImageTag
    }
    docker push $mcpImageTag
    Write-Ok "MCP image pushed"
} else {
    Write-Warn "Skipping image push (-SkipImagePush)"
    Write-Host "  Push images manually:" -ForegroundColor Yellow
    Write-Host "    az acr login --name $($acrLoginServer -split '\.')[0]" -ForegroundColor Gray
    Write-Host "    docker tag agora-cms:latest $acrLoginServer/agora-cms:latest" -ForegroundColor Gray
    Write-Host "    docker push $acrLoginServer/agora-cms:latest" -ForegroundColor Gray
    Write-Host "    docker tag agora-cms-mcp:latest $acrLoginServer/agora-cms-mcp:latest" -ForegroundColor Gray
    Write-Host "    docker push $acrLoginServer/agora-cms-mcp:latest" -ForegroundColor Gray
}

# ── Print summary ────────────────────────────────────────────────

Write-Host ""
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Deployment Complete!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  CMS URL:          $cmsUrl" -ForegroundColor White
Write-Host "  MCP URL:          $mcpUrl" -ForegroundColor White
Write-Host "  ACR:              $acrLoginServer" -ForegroundColor White
Write-Host "  PostgreSQL:       $pgFqdn" -ForegroundColor White
Write-Host "  Key Vault:        $kvUri" -ForegroundColor White
Write-Host "  Storage Account:  $storageName" -ForegroundColor White
Write-Host "  Resource Group:   $ResourceGroup" -ForegroundColor White
Write-Host ""

# ── Save outputs to file for reference ───────────────────────────

$outputFile = Join-Path $scriptDir "deployment-outputs.json"
$outputObj = @{
    timestamp        = (Get-Date -Format "o")
    subscription     = $subInfo.name
    resourceGroup    = $ResourceGroup
    location         = $Location
    prefix           = $Prefix
    cmsUrl           = $cmsUrl
    mcpUrl           = $mcpUrl
    acrLoginServer   = $acrLoginServer
    postgresServerFqdn = $pgFqdn
    keyVaultUri      = $kvUri
    storageAccountName = $storageName
}
$outputObj | ConvertTo-Json -Depth 5 | Set-Content $outputFile -Encoding UTF8
Write-Ok "Outputs saved to $outputFile"

# ── Next steps ───────────────────────────────────────────────────

Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Yellow
Write-Host "    1. Push container images (if skipped): docker push ..." -ForegroundColor Gray
Write-Host "    2. Migrate database: pg_dump / pg_restore to $pgFqdn" -ForegroundColor Gray
Write-Host "    3. Upload assets: azcopy to $storageName blob containers" -ForegroundColor Gray
Write-Host "    4. Store secrets in Key Vault: $kvUri" -ForegroundColor Gray
Write-Host "    5. Open CMS: $cmsUrl" -ForegroundColor Gray
Write-Host ""
