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

Then open `$SERVICE` in a browser and audit a page through the form.

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

**Rate limiting is per-instance only.** The in-process limiter (10/hour/IP by
default, `RATE_LIMIT_PER_HOUR`) resets on cold start and is not shared across
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
