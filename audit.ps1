<#
.SYNOPSIS
  Audit one or more pages through the deployed service and save the PDFs.
  Needs nothing installed locally — the browser work happens on Cloud Run.

.EXAMPLE
  .\audit.ps1 https://example.com
  .\audit.ps1 https://a.com, https://b.com -OutDir .\reports
  $env:AUDIT_SERVICE = "https://website-audit-xxxx.us-central1.run.app"   # set once per window
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string[]]$Url,
  [string]$OutDir  = ".\reports",
  [string]$Service = $env:AUDIT_SERVICE
)

$ErrorActionPreference = "Stop"

if (-not $Service) {
  Write-Host "No service URL. Pass -Service, or set it once for this window:" -ForegroundColor Red
  Write-Host '  $env:AUDIT_SERVICE = "https://website-audit-xxxx.us-central1.run.app"'
  exit 1
}
$Service = $Service.TrimEnd('/')
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

foreach ($target in $Url) {
  Write-Host "Auditing $target ..." -ForegroundColor Cyan -NoNewline
  try {
    # Cold starts plus a heavy page can run past a minute, hence the long timeout.
    $response = Invoke-WebRequest -Uri "$Service/api/audit" -Method Post -TimeoutSec 300 `
      -ContentType "application/json" -Body (@{ url = $target } | ConvertTo-Json)

    $host_    = $response.Headers["X-Audit-Host"]
    $score    = $response.Headers["X-Audit-Score"]
    $grade    = $response.Headers["X-Audit-Grade"]
    $findings = $response.Headers["X-Audit-Findings"]

    $name = ($host_ -replace '[^a-zA-Z0-9.-]', '-')
    $path = Join-Path $OutDir "$name-audit-$(Get-Date -Format yyyy-MM-dd).pdf"
    [IO.File]::WriteAllBytes((Resolve-Path $OutDir).Path + "\" + (Split-Path $path -Leaf), $response.Content)

    Write-Host "`r  $host_ — $score/100 (grade $grade), $findings findings" -ForegroundColor Green
    Write-Host "    $path"
  } catch {
    $status = $_.Exception.Response.StatusCode.value__
    $detail = $_.ErrorDetails.Message
    if ($detail) { try { $detail = ($detail | ConvertFrom-Json).detail } catch { } }
    if ($status -eq 422) {
      Write-Host "`r  $target — site blocked the audit" -ForegroundColor Yellow
    } else {
      Write-Host "`r  $target — failed" -ForegroundColor Red
    }
    if ($detail) { Write-Host "    $detail" }
  }
}
