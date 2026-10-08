# Deploying to Google Cloud Run

The service is one container: a form, an audit endpoint that returns the PDF, and
Chromium. It holds no state between requests, so it scales to zero and back
without a database or shared disk.

> **The image has been built and run.** `docker build` succeeds, the container
> starts as the non-root `pwuser`, Chromium launches, and a full audit — render,
> probe, analysis, 10-page A4 PDF — completes inside it. The one thing not
> exercised locally is fetching a site whose certificate chains to a public root,
> because the build environment re-terminates TLS; that path is what every
> ordinary network does, and Cloud Run is an ordinary network.

## 1. Prerequisites

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install). On Windows
that is the [installer](https://cloud.google.com/sdk/docs/install-sdk#windows) —
and you must **close the terminal and open a new one afterwards**, because the
installer updates PATH and existing terminals do not pick that up.

You do not need Docker locally: Cloud Build builds the image from source.

```bash
gcloud auth login
gcloud projects create redefine-audit --name="Website Audit"   # or use an existing one
gcloud config set project redefine-audit
```

Cloud Run's free tier requires a billing account attached to the project. Attach
one in the console, then **set a budget alert immediately** — it is the only
thing standing between a traffic spike and a surprise bill:

```bash
# Console → Billing → Budgets & alerts → Create budget → $5/month, alert at 50%
```

Enable the three APIs the deploy touches:

```bash
gcloud services enable run.googleapis.com \
                       cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com
```

## 2. Deploy

From the repository root:

```bash
./deploy.sh          # macOS, Linux, or Git Bash on Windows
```

```powershell
.\deploy.ps1         # Windows PowerShell
```

It enables the APIs, builds, deploys, then checks the health endpoint, the SSRF
guard and a real audit before telling you the URL. Safe to re-run — it updates
the service in place. Override the defaults with environment variables:

```bash
REGION=us-central1 MAX_INSTANCES=5 ./deploy.sh
```

```powershell
.\deploy.ps1 -Region us-central1 -MaxInstances 5
```

The equivalent by hand, if you would rather see it:

```bash
gcloud run deploy website-audit \
  --source . \
  --region europe-west2 \
  --memory 2Gi \
  --cpu 2 \
  --concurrency 1 \
  --timeout 300 \
  --min-instances 0 \
  --max-instances 3 \
  --cpu-boost \
  --allow-unauthenticated
```

Cloud Build picks up the `Dockerfile`, pushes the image to Artifact Registry, and
Cloud Run gives you a `https://website-audit-….run.app` URL. First build takes
5–10 minutes (the Playwright base image is large); later builds are much faster.

### Why each flag

| Flag | Reason |
|---|---|
| `--memory 2Gi` | Chromium wants 500 MB–1 GB for a heavy page, plus Python and the PDF. 1 GB is too tight. |
| `--cpu 2` | Rendering and PDF generation are CPU-bound. 1 vCPU roughly doubles audit time. |
| `--concurrency 1` | One audit per instance. Chromium is memory-hungry and the service holds a lock anyway. |
| `--timeout 300` | Audits take 20–60s; 5 minutes leaves room for a slow site without hanging forever. |
| `--max-instances 3` | **The cost ceiling.** Without it a traffic spike scales out and bills you for it. |
| `--min-instances 0` | Scale to zero. Costs nothing idle, at the price of a cold start. |
| `--cpu-boost` | Extra CPU during startup, which shortens the cold start noticeably. |
| `--allow-unauthenticated` | Public. Drop this for an internal-only tool. |

Pick `--region` near the audience you audit for — it is measured in the report's
performance numbers.

### If the first deploy fails with PERMISSION_DENIED

New projects no longer grant their Compute Engine default service account build
permissions, so the first `--source` deploy can fail with
`Build failed because the default service account is missing required IAM
permissions`. Grant it the builder role, wait a minute for IAM to propagate, and
re-run the deploy:

```bash
PROJECT=$(gcloud config get-value project)
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder"
```

If a later run names a different missing permission, add
`roles/artifactregistry.writer` and `roles/logging.logWriter` the same way.

## 3. Check it

```bash
SERVICE=$(gcloud run services describe website-audit --region europe-west2 --format='value(status.url)')

curl -s "$SERVICE/healthz"

# A real audit — should return a PDF and the summary headers
curl -s -D - -o report.pdf -X POST "$SERVICE/api/audit" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}' | grep -i '^x-audit'

# The SSRF guard — must be 400, not a screenshot of the metadata server
curl -s -X POST "$SERVICE/api/audit" \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://169.254.169.254/"}'
```

Then open `$SERVICE` in a browser, choose Website Auditor, and audit a page through
the form (it lives at `$SERVICE/website-audit`).

### Turn on the Post Auditor

The Post Auditor needs two keys. Keep them in Secret Manager rather than plain
environment variables, which anyone with viewer access to the project can read.

- **Apify:** console.apify.com → Settings → API & Integrations → Personal API token.
- **Claude:** console.anthropic.com → API keys. Billed per use, separately from
  any Claude.ai subscription.

```bash
gcloud services enable secretmanager.googleapis.com
printf %s "YOUR_APIFY_TOKEN"   | gcloud secrets create apify-token --data-file=-
printf %s "YOUR_ANTHROPIC_KEY" | gcloud secrets create anthropic-api-key --data-file=-

# Let the service's runtime account read them.
NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
for SECRET in apify-token anthropic-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done

gcloud run services update website-audit --region us-central1 \
  --update-secrets APIFY_TOKEN=apify-token:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest
```

This is a one-off: later `deploy.sh` / `deploy.ps1` runs keep the secrets. To
rotate a key, add a new version (`gcloud secrets versions add apify-token
--data-file=-`) and run the `services update` line again.

Then open `$SERVICE/post-audit` and audit a public TikTok or Instagram post.

## Team sign-in

Off until configured: with none of the three settings below, the app is public
as before. With all three, every page and API call needs a Google account on
the allowed list. With only some, it refuses everything (fails closed) and
says which setting is missing. Plain Gmail accounts work; no Google Workspace
needed, and no accounts database — Google holds the accounts.

1. **Consent screen.** Console → Google Auth Platform (APIs & Services → OAuth
   consent screen) → Get started: app name "Redefine App", your email as
   support contact, audience **External**. Under Audience, **Publish app**:
   sign-in only asks for name and email, which needs no Google review.
2. **Client ID.** Clients → Create client → **Web application**. Under
   *Authorised JavaScript origins* add the service URL, e.g.
   `https://website-audit-1015165980124.us-central1.run.app` (and any custom
   domain later). No redirect URIs are needed. Copy the client ID.
3. **Session secret.** Any long random string. In PowerShell:

   ```powershell
   $b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)
   ```

4. **Turn it on.** The `^;^` prefix makes `;` the separator, because the email
   list itself contains commas:

   ```bash
   gcloud run services update website-audit --region us-central1 \
     --update-env-vars "^;^GOOGLE_CLIENT_ID=YOUR_ID.apps.googleusercontent.com;ALLOWED_EMAILS=you@gmail.com,teammate@gmail.com;SESSION_SECRET=YOUR_SECRET"
   ```

`ALLOWED_EMAILS` are the **owners**: they can always sign in and manage the
team, and can't be removed from the admin page, so nobody can lock everyone
out from inside the app. Add everyone else on the admin page (below). Changing
`SESSION_SECRET` signs everyone out.

### Team admin page

Owners and admins manage everyone else at `/admin/team` (the **Team** link next
to Sign out): add a Gmail as member (uses the tools) or admin (also manages the
team), or remove someone; removal applies on their next click. The list lives
in Firestore, collection `team_members`, in this same project.

One-time setup: Console → **Firestore** → **Create database** → Native mode,
database ID `(default)`, location `us-central1`, **production** security rules
(browsers never talk to Firestore directly; the app reaches it as the Cloud Run
service account, which the default Compute Engine account already allows — a
custom service account needs `roles/datastore.user`). Until the database exists,
owners can still sign in and the page explains what is missing.

### Audit history

Every finished Post Auditor run is saved to the same Firestore database,
collection `post_audits`: who ran it, when, the post's numbers, the full
analysis, and the frames re-encoded at 320px (about 300 KB per audit, well
inside Firestore's 1 MB document limit). `/history` lists them newest first
with search, a platform filter and "Only mine"; opening one shows the full
report again, and its PDF is rebuilt from the saved copy on download. The
person who ran an audit, or any owner or admin, can delete it.

No extra setup beyond the Firestore database above. Saving is best effort: if
Firestore is missing or down, audits still work, they just are not saved, and
the history page says so. Firestore's free tier (1 GiB, 20K writes and 50K
reads a day) covers a few thousand audits.

## Automatic deploys

`cloudbuild.yaml` deploys on every push: it builds the image, runs the whole
test suite inside it, and only then rolls it out to Cloud Run. A push that
fails a test stops at the test step and the live site is untouched.
Environment variables and secrets carry over from the running revision.

Set it up once:

1. **Grant the build permissions.** Cloud Build runs as the Compute Engine
   default service account; let it deploy and push images:

   ```bash
   PROJECT=$(gcloud config get-value project)
   NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
   SA="${NUMBER}-compute@developer.gserviceaccount.com"
   for ROLE in roles/run.developer roles/iam.serviceAccountUser \
               roles/artifactregistry.writer roles/logging.logWriter; do
     gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$ROLE"
   done
   ```

2. **Connect GitHub.** Console → Cloud Build → Triggers → region
   `us-central1` → **Connect repository** → GitHub (Cloud Build GitHub App) →
   authorise, then pick `godelegate2025/RDclaudecode`.

3. **Create the trigger.** Same page → **Create trigger**:
   - Event: **Push to a branch**
   - Repository: `godelegate2025/RDclaudecode`
   - Branch: `^claude/keen-clarke-euqbmv$` (the default branch)
   - Configuration: **Cloud Build configuration file**, location `/cloudbuild.yaml`
   - Service account: the Compute Engine default service account from step 1

4. **Test it.** On the trigger, click **Run**. Watch Cloud Build → History: the
   steps are build (~8–12 min the first time), test, push, deploy.

A build takes roughly 10 minutes and fits in Cloud Build's free monthly
minutes. `deploy.sh` / `deploy.ps1` still work for a manual deploy.

## 4. Costs

Cloud Run's monthly free tier is, at the time of writing, 2M requests, 180,000
vCPU-seconds and 360,000 GiB-seconds — verify current limits, they move.

At the settings above (2 vCPU, 2 GiB) a 30-second audit consumes roughly 60
vCPU-seconds and 60 GiB-seconds, so the binding constraint is vCPU:

**≈ 3,000 audits per month within the free tier.** Past that it is a few
thousandths of a dollar per audit. `--max-instances 3` caps the worst case.

## 5. Hardening before you publicise it

**The SSRF guard is already in** (`service/security.py`) — every URL is resolved
and checked against private, loopback, link-local and metadata ranges before
Chromium sees it, and re-checked on each redirect. Do not remove it.

**Rate limiting is per-instance only.** The in-process limiter (30 units/hour/IP
by default, `RATE_LIMIT_PER_HOUR`; a site audit costs 5, a page audit 1, failed
audits are refunded) resets on cold start and is not shared across
instances. For real protection put Cloud Armor in front:

```bash
gcloud compute security-policies create audit-policy \
  --description "Rate limit the public audit endpoint"

gcloud compute security-policies rules create 100 \
  --security-policy audit-policy \
  --expression "true" \
  --action throttle \
  --rate-limit-threshold-count 10 \
  --rate-limit-threshold-interval-sec 60 \
  --conform-action allow \
  --exceed-action deny-429 \
  --enforce-on-key IP
```

Cloud Armor attaches through a load balancer, which is also what you need for a
custom domain — worth doing both at once.

**Chromium runs with `--no-sandbox`** because its own sandbox needs privileges
Cloud Run does not grant. Cloud Run runs every container inside gVisor, and that
is the real isolation boundary; the flag stops Chromium trying to nest a second
sandbox inside it and failing to start. Do not carry that flag to a host where
gVisor or equivalent isn't doing that job.

## 6. Custom domain

```bash
gcloud beta run domain-mappings create \
  --service website-audit \
  --domain audit.yourdomain.com \
  --region europe-west2
```

Add the DNS records it prints. Certificates are issued automatically.

## 7. Watching it

```bash
gcloud run services logs read website-audit --region europe-west2 --limit 50
```

Each completed audit logs a line with the host, duration, score and finding
count. Failures log a full traceback.

## Running it locally first

```bash
pip install -r requirements.txt -r requirements-service.txt
python -m playwright install chromium
uvicorn service.app:app --reload --port 8000
```

Then open http://127.0.0.1:8000.
