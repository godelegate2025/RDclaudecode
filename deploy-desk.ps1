<#
.SYNOPSIS
  Deploy the Care Desk to Google Cloud Run from Windows PowerShell.

.DESCRIPTION
  Builds Dockerfile.desk and deploys it as its own service next to the Redefine
  App. Sign-in is the app's: the script copies GOOGLE_CLIENT_ID, ALLOWED_EMAILS
  and SESSION_SECRET from the running website-audit service, and the desk reads
  the same team list from the same Firestore database. It also creates the
  webhook and cron keys, and the 5-minute Cloud Scheduler job that escalates
  late tickets and sends journey emails. Safe to re-run.

  Before the first run: team sign-in and Firestore must already be set up for
  the Redefine App (DEPLOY.md, "Team sign-in").

  Optional, for real emails:
    "re_..." | gcloud secrets create resend-api-key --data-file=-

.EXAMPLE
  .\deploy-desk.ps1 -FromEmail "Redefine <hello@redefine.ph>" -ReplyTo "hello@redefine.ph"
#>
[CmdletBinding()]
param(
  [string]$Service    = "care-desk",
  [string]$AppService = "redefine-apps",
  [string]$Region     = "us-central1",
  [string]$Project    = "",
  [string]$FromEmail  = "",
  [string]$ReplyTo    = ""
)

# gcloud reports progress on stderr, which Windows PowerShell turns into errors
# when output is redirected; every step checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
function Write-Step($text) { Write-Host $text -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { Fail "gcloud is not installed. See DEPLOY.md." }
if (-not (gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null)) { Fail "Not signed in. Run: gcloud auth login" }
if (-not $Project) { $Project = (gcloud config get-value project 2>$null) }
if (-not $Project -or $Project -eq "(unset)") { Fail "No project set. Run: gcloud config set project YOUR_PROJECT_ID" }

Write-Step "Deploying '$Service' to $Region in project $Project"

Write-Step "1/6  Enabling APIs (no-op if already enabled)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  secretmanager.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com --project $Project
if ($LASTEXITCODE) { Fail "Could not enable the APIs." }

Write-Step "2/6  Reading the app's sign-in settings and checking secrets"
$appJson = gcloud run services describe $AppService --region $Region --project $Project --format=json 2>$null
if ($LASTEXITCODE -or -not $appJson) { Fail "Could not read the '$AppService' service in $Region. Deploy the Redefine App first." }
$appEnv = @{}
foreach ($e in (($appJson | ConvertFrom-Json).spec.template.spec.containers[0].env)) { if ($e.value) { $appEnv[$e.name] = $e.value } }
foreach ($name in "GOOGLE_CLIENT_ID", "ALLOWED_EMAILS", "SESSION_SECRET") {
  if (-not $appEnv[$name]) { Fail "$AppService has no $name. Turn on team sign-in first (DEPLOY.md, Team sign-in)." }
}

function Test-Secret($name) {
  gcloud secrets describe $name --project $Project 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}
function New-RandomSecret($name) {
  $bytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $value = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  $tmp = New-TemporaryFile
  try {
    [IO.File]::WriteAllText($tmp, $value)
    gcloud secrets create $name --data-file=$tmp --project $Project | Out-Null
  } finally { Remove-Item $tmp -Force }
  Write-Host "  created $name"
}
foreach ($name in "desk-webhook-key", "desk-cron-key") {
  if (-not (Test-Secret $name)) { New-RandomSecret $name }
}
$secrets = "DESK_WEBHOOK_KEY=desk-webhook-key:latest,DESK_CRON_KEY=desk-cron-key:latest"
$copyClaudeKey = $false
if (Test-Secret "anthropic-api-key") { $secrets += ",ANTHROPIC_API_KEY=anthropic-api-key:latest" }
elseif ($appEnv["ANTHROPIC_API_KEY"]) { $copyClaudeKey = $true; Write-Host "  using the Claude key set on $AppService" }
else { Write-Host "  no Claude key found: messages will be sorted by keyword rules" -ForegroundColor Yellow }
if (Test-Secret "resend-api-key") { $secrets += ",RESEND_API_KEY=resend-api-key:latest" }
else { Write-Host "  resend-api-key not found: emails are logged, not sent" -ForegroundColor Yellow }

# Cloud Run runs as the default compute service account; it needs to read the secrets.
$number = gcloud projects describe $Project --format="value(projectNumber)"
$runAs = "$number-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$runAs" `
  --role="roles/secretmanager.secretAccessor" --condition=None --quiet | Out-Null

Write-Step "3/6  Building the image"
$repo = "cloud-run-source-deploy"
gcloud artifacts repositories describe $repo --location $Region --project $Project 2>$null | Out-Null
if ($LASTEXITCODE) {
  gcloud artifacts repositories create $repo --repository-format=docker --location $Region --project $Project | Out-Null
}
$image = "$Region-docker.pkg.dev/$Project/$repo/${Service}:latest"
gcloud builds submit --config cloudbuild.desk.yaml --substitutions "_IMAGE=$image" --project $Project .
if ($LASTEXITCODE) { Fail "The build failed." }

Write-Step "4/6  Deploying"
# Public on purpose: GHL must reach the webhook, and every page needs team sign-in.
# '|' separates the variables because ALLOWED_EMAILS has commas and FromEmail an '@'.
$vars = @("GOOGLE_CLIENT_ID=$($appEnv['GOOGLE_CLIENT_ID'])", "ALLOWED_EMAILS=$($appEnv['ALLOWED_EMAILS'])",
          "SESSION_SECRET=$($appEnv['SESSION_SECRET'])")
if ($appEnv["FIRESTORE_DATABASE"]) { $vars += "FIRESTORE_DATABASE=$($appEnv['FIRESTORE_DATABASE'])" }
if ($copyClaudeKey) { $vars += "ANTHROPIC_API_KEY=$($appEnv['ANTHROPIC_API_KEY'])" }
if ($FromEmail) { $vars += "DESK_FROM_EMAIL=$FromEmail" }
if ($ReplyTo) { $vars += "DESK_REPLY_TO=$ReplyTo" }
gcloud run deploy $Service --image $image --project $Project --region $Region `
  --memory 512Mi --cpu 1 --concurrency 20 --timeout 120 --min-instances 0 --max-instances 2 `
  --set-secrets $secrets --update-env-vars "^|^$($vars -join '|')" --allow-unauthenticated
if ($LASTEXITCODE) { Fail "The deploy failed." }

$url = gcloud run services describe $Service --project $Project --region $Region --format="value(status.url)"
# Unsubscribe links are built from this address.
gcloud run services update $Service --project $Project --region $Region --update-env-vars "DESK_PUBLIC_URL=$url" | Out-Null

Write-Step "5/6  Scheduling the clock (every 5 minutes)"
$cronKey = gcloud secrets versions access latest --secret desk-cron-key --project $Project
$job = "$Service-tick"
gcloud scheduler jobs describe $job --location $Region --project $Project 2>$null | Out-Null
$verb = if ($LASTEXITCODE) { "create" } else { "update" }
$headerFlag = if ($verb -eq "create") { "--headers" } else { "--update-headers" }
gcloud scheduler jobs $verb http $job --location $Region --project $Project --schedule "*/5 * * * *" `
  --uri "$url/api/desk/tick" --http-method POST $headerFlag "X-Cron-Key=$cronKey" | Out-Null
if ($LASTEXITCODE) { Fail "Could not set up the scheduler job." }

Write-Step "6/6  Checking the deployment"
# Not /healthz: Cloud Run's front end reserves that path and never passes it on.
# The sign-in page answers 200 only when sign-in is fully set up (503 otherwise).
try {
  $page = Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 60
  if ($page.StatusCode -ne 200) { Fail "The sign-in page answered $($page.StatusCode)." }
} catch { Fail "The desk did not answer at $url/login : $($_.Exception.Message)" }

Write-Host ""
Write-Host "Care Desk is live: $url/desk" -ForegroundColor Green
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Google sign-in: add $url to the OAuth client's Authorised JavaScript origins"
Write-Host "     (Console -> Google Auth Platform -> Clients -> your client)."
Write-Host "  2. GHL webhook URL: $url/hooks/ghl?key=<run: gcloud secrets versions access latest --secret desk-webhook-key>"
Write-Host "  3. Link it from the Redefine App:"
Write-Host "     gcloud run services update $AppService --region $Region --update-env-vars CARE_DESK_URL=$url/desk"
