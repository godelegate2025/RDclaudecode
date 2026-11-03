<#
.SYNOPSIS
  Create or update a Care Desk on Google Cloud Run, one per business.

.DESCRIPTION
  Each desk serves one business and is its own Cloud Run service, set up from
  care_desk/clients/<client>.json (copy _example.json for a new client). The
  same image runs every desk; DESK_CLIENT tells it which business it is for.

  Redefine's own desk ("redefine", service care-desk) shares sign-in with the
  Redefine App: the script copies GOOGLE_CLIENT_ID, ALLOWED_EMAILS,
  SESSION_SECRET and FIRESTORE_DATABASE from the running redefine-apps service.

  A client's desk shares only the Google sign-in button (GOOGLE_CLIENT_ID) and
  the sender of sign-in codes. Its owners are deploy.owners in its client file,
  its session secret is its own (made here), and its data, people's names and
  sign-in codes live in its own Supabase database. Create that secret first:
    Get-Clipboard | gcloud secrets create <service>-db-url --data-file=-
  (the Supabase "Session pooler" connection string; CARE_DESK.md, "New client desk").

  The script also makes each desk's webhook and cron keys and its 5-minute
  Cloud Scheduler job. Safe to re-run: later runs keep what is set.

  -All builds the image once and updates every desk in care_desk/clients.
  -AutoDeploy sets up a Cloud Build trigger that does the same on every push.

.EXAMPLE
  .\deploy-desk.ps1
  Updates Redefine's own desk.

.EXAMPLE
  .\deploy-desk.ps1 -Client acme
  Creates (or updates) the desk set out in care_desk/clients/acme.json.

.EXAMPLE
  .\deploy-desk.ps1 -All
  Builds once, runs the tests, and updates every client's desk.

.EXAMPLE
  .\deploy-desk.ps1 -AutoDeploy
  From now on, every push that touches the desk updates every desk that exists.
#>
[CmdletBinding()]
param(
  [string]$Client     = "redefine",
  [switch]$All,
  [switch]$AutoDeploy,
  [string]$AppService = "redefine-apps",
  [string]$Region     = "us-central1",
  [string]$Project    = "",
  # Overrides for one run; the client file is the place to keep them.
  [string]$PublicUrl  = ""
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

$clientsDir = Join-Path $PSScriptRoot "care_desk/clients"
$image = "$Region-docker.pkg.dev/$Project/cloud-run-source-deploy/care-desk:latest"

function Read-Client($name) {
  if ($name -notmatch '^[a-z0-9][a-z0-9-]*$') { Fail "-Client is a file name in care_desk/clients, like 'redefine'." }
  $path = Join-Path $clientsDir "$name.json"
  if (-not (Test-Path $path)) { Fail "No care_desk/clients/$name.json. Copy _example.json to it and fill it in." }
  $c = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($field in "brand", "about", "services") { if (-not $c.$field) { Fail "$name.json has no $field." } }
  if (-not $c.deploy) { Fail "$name.json has no deploy section." }
  return $c
}

# Every desk in care_desk/clients, by service name; _example.json and other _ files are not desks.
function All-Clients {
  Get-ChildItem $clientsDir -Filter "*.json" | Where-Object { $_.Name -notlike "_*" } | ForEach-Object { $_.BaseName }
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

# ------------------------------------------------------------------ automatic deploys

if ($AutoDeploy) {
  # Same GitHub repository, branch and service account as the app's own trigger.
  Write-Step "Setting up automatic desk deploys"
  $appTrigger = gcloud builds triggers list --region $Region --project $Project --format=json 2>$null | ConvertFrom-Json |
    Where-Object { $_.filename -eq "cloudbuild.yaml" } | Select-Object -First 1
  if (-not $appTrigger -or -not $appTrigger.github) { Fail "Could not find the app's trigger (cloudbuild.yaml) in $Region. See DEPLOY.md, Automatic deploys." }
  $name = "deploy-desks-on-push"
  gcloud builds triggers describe $name --region $Region --project $Project 2>$null | Out-Null
  if (-not $LASTEXITCODE) { Write-Host "  $name already exists: nothing to do." -ForegroundColor Green; exit 0 }
  # Only pushes that change the desk, or the shared sign-in code it ships with.
  $files = "care_desk/**,service/__init__.py,service/auth.py,service/team.py,service/firestore_db.py," +
           "service/email_login.py,service/profiles.py,Dockerfile.desk,cloudbuild.desk.yaml,requirements-desk.txt," +
           "tests/test_care_desk.py,tests/__init__.py"
  gcloud builds triggers create github --name $name --region $Region --project $Project `
    --repo-owner $appTrigger.github.owner --repo-name $appTrigger.github.name `
    --branch-pattern $appTrigger.github.push.branch --build-config cloudbuild.desk.yaml `
    --service-account $appTrigger.serviceAccount --included-files $files `
    --substitutions "_IMAGE=$image,_REGION=$Region,_ROLL_OUT=yes"
  if ($LASTEXITCODE) { Fail "Could not create the trigger." }
  Write-Host "  every push to $($appTrigger.github.push.branch) that changes the desk now updates every desk" -ForegroundColor Green
  Write-Host "  (new client desks still start with: .\deploy-desk.ps1 -Client <name>)"
  exit 0
}

# ------------------------------------------------------------------ build once

$names = if ($All) { @(All-Clients) } else { @($Client) }
$clients = @{}
foreach ($n in $names) { $clients[$n] = Read-Client $n }
if ($All -and $PublicUrl) { Fail "-PublicUrl is for one desk; set deploy.public_url in each client file instead." }

Write-Step "Desks: $($names -join ', ')  ($Region, project $Project)"

Write-Step "Enabling APIs (no-op if already enabled)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  secretmanager.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com --project $Project
if ($LASTEXITCODE) { Fail "Could not enable the APIs." }

$appJson = gcloud run services describe $AppService --region $Region --project $Project --format=json 2>$null
if ($LASTEXITCODE -or -not $appJson) { Fail "Could not read the '$AppService' service in $Region. Deploy the Redefine App first." }
$appEnv = @{}
foreach ($e in (($appJson | ConvertFrom-Json).spec.template.spec.containers[0].env)) { if ($e.value) { $appEnv[$e.name] = $e.value } }
if (-not $appEnv["GOOGLE_CLIENT_ID"]) { Fail "$AppService has no GOOGLE_CLIENT_ID. Turn on team sign-in first (DEPLOY.md, Team sign-in)." }

# Cloud Run runs as the default compute service account; it needs to read the secrets.
$number = gcloud projects describe $Project --format="value(projectNumber)"
gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$number-compute@developer.gserviceaccount.com" `
  --role="roles/secretmanager.secretAccessor" --condition=None --quiet | Out-Null

Write-Step "Building the image and running the desk's tests (once for every desk)"
$repo = "cloud-run-source-deploy"
gcloud artifacts repositories describe $repo --location $Region --project $Project 2>$null | Out-Null
if ($LASTEXITCODE) {
  gcloud artifacts repositories create $repo --repository-format=docker --location $Region --project $Project | Out-Null
}
# Builds the image and runs the desk's tests in it; nothing is pushed unless they pass.
gcloud builds submit --config cloudbuild.desk.yaml --substitutions "_IMAGE=$image,_REGION=$Region" --project $Project .
if ($LASTEXITCODE) { Fail "The build or the desk's tests failed. Nothing was deployed; the log above says which." }

# ------------------------------------------------------------------ one desk

function Deploy-Desk($name, $c) {
  $d = $c.deploy
  $service = if ($d.service) { $d.service } else { "care-desk-$name" }
  $own = -not $c.with_redefine_app
  Write-Step "`n== $($c.brand): $service =="

  # Secret names: Redefine's desk keeps the ones it was set up with; a client's start with its service name.
  $secretNames = @{ webhook = "$service-webhook-key"; cron = "$service-cron-key"; db = "$service-db-url"
              meta = "$service-meta-page-token"; session = "$service-session-secret" }
  if ($d.secrets) { foreach ($p in $d.secrets.PSObject.Properties) { $secretNames[$p.Name] = $p.Value } }

  foreach ($kind in "webhook", "cron") { if (-not (Test-Secret $secretNames[$kind])) { New-RandomSecret $secretNames[$kind] } }
  $secrets = "DESK_WEBHOOK_KEY=$($secretNames.webhook):latest,DESK_CRON_KEY=$($secretNames.cron):latest"
  $vars = @("DESK_CLIENT=$name", "GOOGLE_CLIENT_ID=$($appEnv['GOOGLE_CLIENT_ID'])")

  if ($own) {
    $owners = @($d.owners | Where-Object { $_ -match '^[^@\s]+@[^@\s]+\.[^@\s]+$' })
    if (-not $owners) { Fail "$name.json needs deploy.owners: the emails that run this desk and can never be locked out." }
    if (-not (Test-Secret $secretNames.session)) { New-RandomSecret $secretNames.session }
    $secrets += ",SESSION_SECRET=$($secretNames.session):latest"
    $vars += "ALLOWED_EMAILS=$($owners -join ',')"
    if (-not (Test-Secret $secretNames.db)) {
      Fail ("$($c.brand)'s desk needs its own database. Copy its Supabase connection string, then run:`n" +
            "  Get-Clipboard | gcloud secrets create $($secretNames.db) --data-file=-`nand run this again.")
    }
    $secrets += ",SUPABASE_DB_URL=$($secretNames.db):latest"
    if ($d.db_schema) { $vars += "DESK_DB_SCHEMA=$($d.db_schema)" }
    Write-Host "  sign-in: its own (owners: $($owners -join ', '))"
  } else {
    foreach ($n in "ALLOWED_EMAILS", "SESSION_SECRET") {
      if (-not $appEnv[$n]) { Fail "$AppService has no $n. Turn on team sign-in first (DEPLOY.md, Team sign-in)." }
    }
    $vars += "ALLOWED_EMAILS=$($appEnv['ALLOWED_EMAILS'])", "SESSION_SECRET=$($appEnv['SESSION_SECRET'])"
    if ($appEnv["FIRESTORE_DATABASE"]) { $vars += "FIRESTORE_DATABASE=$($appEnv['FIRESTORE_DATABASE'])" }
    if (Test-Secret $secretNames.db) { $secrets += ",SUPABASE_DB_URL=$($secretNames.db):latest" }
    Write-Host "  sign-in: shared with $AppService"
  }

  # Shared by every desk: Redefine's Claude key and Resend account, and the sender of sign-in codes.
  if (Test-Secret "anthropic-api-key") { $secrets += ",ANTHROPIC_API_KEY=anthropic-api-key:latest" }
  elseif ($appEnv["ANTHROPIC_API_KEY"]) { $vars += "ANTHROPIC_API_KEY=$($appEnv['ANTHROPIC_API_KEY'])" }
  else { Write-Host "  no Claude key found: messages will be sorted by keyword rules" -ForegroundColor Yellow }
  if (Test-Secret "resend-api-key") { $secrets += ",RESEND_API_KEY=resend-api-key:latest" }
  else { Write-Host "  resend-api-key not found: emails are logged, not sent" -ForegroundColor Yellow }
  if ($appEnv["LOGIN_FROM_EMAIL"]) { $vars += "LOGIN_FROM_EMAIL=$($appEnv['LOGIN_FROM_EMAIL'])" }

  if (Test-Secret $secretNames.meta) {
    $secrets += ",META_PAGE_TOKEN=$($secretNames.meta):latest"
    if ($d.meta_page_id) { $vars += "META_PAGE_ID=$($d.meta_page_id)" }
    elseif ($own) { Write-Host "  $($secretNames.meta) is set but deploy.meta_page_id is not: no Facebook replies" -ForegroundColor Yellow }
  }
  if ($d.from_email) { $vars += "DESK_FROM_EMAIL=$($d.from_email)" }
  if ($d.reply_to) { $vars += "DESK_REPLY_TO=$($d.reply_to)" }
  $min = if ($null -ne $d.min_instances) { [int]$d.min_instances } else { 0 }

  # Public on purpose: GHL and n8n must reach the webhooks, and every page needs team sign-in.
  # '|' separates the variables because ALLOWED_EMAILS has commas and the senders an '@'.
  gcloud run deploy $service --image $image --project $Project --region $Region `
    --memory 512Mi --cpu 1 --concurrency 20 --timeout 120 --min-instances $min --max-instances 2 `
    --set-secrets $secrets --update-env-vars "^|^$($vars -join '|')" --allow-unauthenticated
  if ($LASTEXITCODE) { Fail "The deploy of $service failed." }

  $deskJson = gcloud run services describe $service --project $Project --region $Region --format=json | ConvertFrom-Json
  $url = $deskJson.status.url
  # Unsubscribe links and invites use this address: -PublicUrl, then the client file,
  # then what is already set, then the service's own address.
  $current = ($deskJson.spec.template.spec.containers[0].env | Where-Object { $_.name -eq "DESK_PUBLIC_URL" }).value
  $public = if ($PublicUrl) { $PublicUrl.TrimEnd("/") } elseif ($d.public_url) { $d.public_url.TrimEnd("/") }
            elseif ($current) { $current } else { $url }
  if ($public -ne $current) {
    gcloud run services update $service --project $Project --region $Region --update-env-vars "DESK_PUBLIC_URL=$public" | Out-Null
  }

  $cronKey = gcloud secrets versions access latest --secret $secretNames.cron --project $Project
  $job = "$service-tick"
  gcloud scheduler jobs describe $job --location $Region --project $Project 2>$null | Out-Null
  $verb = if ($LASTEXITCODE) { "create" } else { "update" }
  $headerFlag = if ($verb -eq "create") { "--headers" } else { "--update-headers" }
  gcloud scheduler jobs $verb http $job --location $Region --project $Project --schedule "*/5 * * * *" `
    --uri "$url/api/desk/tick" --http-method POST $headerFlag "X-Cron-Key=$cronKey" | Out-Null
  if ($LASTEXITCODE) { Fail "Could not set up the scheduler job for $service." }

  # Not /healthz: Cloud Run's front end reserves that path and never passes it on. Not a page
  # either: pages on this address redirect to the public one. Signed out, the desk's API answers
  # 401 when sign-in is fully set up, and 503 when it is not.
  $status = 0
  try { $status = (Invoke-WebRequest "$url/api/desk/state" -UseBasicParsing -TimeoutSec 60).StatusCode }
  catch { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } else { $err = $_.Exception.Message } }
  if ($status -ne 401) {
    if ($status) { Fail "$service answered $status at $url/api/desk/state (401 expected). Its logs say why." }
    Fail "$service did not answer at $url : $err. Its logs say why."
  }

  Write-Host "$($c.brand)'s desk is live: $public" -ForegroundColor Green
  return @{ service = $service; public = $public; webhook = $secretNames.webhook; own = $own }
}

$done = foreach ($n in $names) { Deploy-Desk $n $clients[$n] }

Write-Host ""
Write-Host "Next, for a desk that is new:"
Write-Host "  1. Google sign-in: add its address to the OAuth client's Authorised JavaScript origins"
Write-Host "     (Console -> Google Auth Platform -> Clients -> your client)."
Write-Host "  2. GHL and n8n webhooks: <address>/hooks/ghl and <address>/hooks/email, with ?key=<its webhook key>"
Write-Host "     (gcloud secrets versions access latest --secret <service>-webhook-key)."
foreach ($r in @($done | Where-Object { $_ -is [hashtable] })) { Write-Host "     $($r.public)  ->  key secret $($r.webhook)" }
