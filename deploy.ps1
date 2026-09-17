<#
.SYNOPSIS
  Deploy the audit service to Google Cloud Run from Windows PowerShell.

.EXAMPLE
  .\deploy.ps1
  .\deploy.ps1 -Region us-central1
  .\deploy.ps1 -Region us-central1 -MaxInstances 5

  Safe to re-run: it updates the existing service in place.
#>
[CmdletBinding()]
param(
  [string]$Service      = "website-audit",
  [string]$Region       = "europe-west2",
  [int]   $MaxInstances = 3,
  [string]$Project      = ""
)

$ErrorActionPreference = "Stop"

function Write-Step($text) { Write-Host $text -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  Fail @"
gcloud is not installed, or this terminal was opened before installing it.

  Install:  https://cloud.google.com/sdk/docs/install-sdk#windows
  Then CLOSE this window and open a new PowerShell — the installer updates PATH
  and existing terminals do not pick that up.
"@
}

$active = (gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null)
if (-not $active) { Fail "Not signed in. Run: gcloud auth login" }

if (-not $Project) { $Project = (gcloud config get-value project 2>$null) }
if (-not $Project -or $Project -eq "(unset)") {
  Fail "No project set. Run: gcloud config set project YOUR_PROJECT_ID"
}

Write-Step "Deploying '$Service' to $Region in project $Project"
Write-Host ""

Write-Step "1/3  Enabling APIs (no-op if already enabled)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
                       artifactregistry.googleapis.com --project $Project
if ($LASTEXITCODE -ne 0) { Fail "Could not enable the APIs. Is billing attached to $Project?" }

Write-Step "2/3  Building and deploying (first build takes 5-10 minutes)"
# --max-instances is the cost ceiling: without it a traffic spike scales out and
# bills for every instance. --concurrency 1 because each instance runs one
# Chromium at a time.
gcloud run deploy $Service `
  --source . `
  --project $Project `
  --region $Region `
  --memory 2Gi `
  --cpu 2 `
  --concurrency 1 `
  --timeout 600 `
  --min-instances 0 `
  --max-instances $MaxInstances `
  --cpu-boost `
  --allow-unauthenticated
if ($LASTEXITCODE -ne 0) { Fail "Deploy failed. See the build log link above." }

# Cloud Run assigns more than one URL and status.url can hand back the legacy
# .a.run.app form, which may not resolve. Try every URL it reports.
$listed = (gcloud run services describe $Service --project $Project --region $Region `
           --format="value(status.urls)")
$single = (gcloud run services describe $Service --project $Project --region $Region `
           --format="value(status.url)")
$candidates = @($listed, $single) -join ";" -split "[;,\s]+" |
  Where-Object { $_ -match "^https://" } | Select-Object -Unique
if (-not $candidates) { Fail "Could not read the service URL from gcloud." }

Write-Step "3/3  Checking the deployment"

# A freshly deployed URL is not routable for up to a minute, so poll rather than
# reporting Google's frontend 404 as a failure.
$url = $null
foreach ($attempt in 1..30) {
  foreach ($candidate in $candidates) {
    try {
      Invoke-RestMethod -Uri "$candidate/healthz" -TimeoutSec 20 | Out-Null
      $url = $candidate
      break
    } catch { }
  }
  if ($url) { break }
  Start-Sleep -Seconds 4
}
if ($url) { Write-Host "  health:      ok" }
else {
  Write-Host "  health:      no response" -ForegroundColor Red
  Fail ("  No service URL answered in 2 minutes. Tried:`n    " + ($candidates -join "`n    ") +
        "`n  The deploy itself succeeded - try those in a browser before assuming it is broken.")
}

# The guard must refuse the cloud metadata address. PowerShell throws on 4xx,
# so a 400 here is the success path.
$guarded = $false
try {
  Invoke-RestMethod -Uri "$url/api/audit" -Method Post -TimeoutSec 60 `
    -ContentType "application/json" -Body '{"url":"http://169.254.169.254/"}' | Out-Null
} catch {
  if ($_.Exception.Response.StatusCode.value__ -eq 400) { $guarded = $true }
}
if ($guarded) { Write-Host "  SSRF guard:  ok (metadata address refused)" }
else { Fail "  SSRF guard did not return 400 - do not expose this service publicly" }

try {
  $response = Invoke-WebRequest -Uri "$url/api/audit" -Method Post -TimeoutSec 300 `
    -ContentType "application/json" -Body '{"url":"https://example.com","mode":"page"}'
  $score = $response.Headers["X-Audit-Score"]
  if ($score) { Write-Host "  live audit:  ok (example.com scored $score/100)" }
  else { Fail "  audit returned no score header" }
} catch {
  Write-Host "  live audit:  FAILED" -ForegroundColor Red
  Fail "  Check the logs: gcloud run services logs read $Service --region $Region --limit 50"
}

Write-Host ""
Write-Step "Deployed: $url"
Write-Host "Open that URL to use the form."
Write-Host ""
Write-Host "Logs:  gcloud run services logs read $Service --region $Region --limit 50"
Write-Host "Stop:  gcloud run services delete $Service --region $Region"
