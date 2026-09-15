#!/usr/bin/env bash
# Deploy the audit service to Google Cloud Run.
#
#   ./deploy.sh                          # uses your current gcloud project
#   REGION=us-central1 ./deploy.sh       # pick a region near your audience
#   SERVICE=audit-staging ./deploy.sh    # deploy a second copy
#
# Safe to re-run: it updates the existing service in place.

set -euo pipefail

SERVICE="${SERVICE:-website-audit}"
REGION="${REGION:-europe-west2}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

command -v gcloud >/dev/null || fail \
  "gcloud is not installed. See https://cloud.google.com/sdk/docs/install"

gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || fail \
  "Not signed in. Run: gcloud auth login"

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ] || fail \
  "No project set. Run: gcloud config set project YOUR_PROJECT_ID"

bold "Deploying '$SERVICE' to $REGION in project $PROJECT"
echo

bold "1/3  Enabling APIs (no-op if already enabled)"
gcloud services enable run.googleapis.com \
                       cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com \
                       --project "$PROJECT"

bold "2/3  Building and deploying (first build takes 5-10 minutes)"
# --max-instances is the cost ceiling: without it a traffic spike scales out and
# bills for every instance. --concurrency 1 because each instance runs one
# Chromium at a time.
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --memory 2Gi \
  --cpu 2 \
  --concurrency 1 \
  --timeout 300 \
  --min-instances 0 \
  --max-instances "$MAX_INSTANCES" \
  --cpu-boost \
  --allow-unauthenticated

URL="$(gcloud run services describe "$SERVICE" \
        --project "$PROJECT" --region "$REGION" --format='value(status.url)')"

bold "3/3  Checking the deployment"

printf '  health:      '
curl -fsS "$URL/healthz" >/dev/null && echo "ok" || fail "health check failed"

printf '  SSRF guard:  '
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/api/audit" \
        -H 'Content-Type: application/json' \
        -d '{"url":"http://169.254.169.254/"}')"
[ "$CODE" = "400" ] && echo "ok (metadata address refused)" \
  || fail "SSRF guard returned $CODE, expected 400 — do not expose this publicly"

printf '  live audit:  '
HEADERS="$(curl -s -D - -o /dev/null -X POST "$URL/api/audit" \
           -H 'Content-Type: application/json' \
           -d '{"url":"https://example.com"}')"
SCORE="$(printf '%s' "$HEADERS" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-audit-score"{print $2}')"
[ -n "$SCORE" ] && echo "ok (example.com scored $SCORE/100)" \
  || { printf '%s\n' "$HEADERS" | head -1
       fail "audit failed — check: gcloud run services logs read $SERVICE --region $REGION --limit 50"; }

echo
bold "Deployed: $URL"
echo "Open that URL to use the form."
echo
echo "Logs:  gcloud run services logs read $SERVICE --region $REGION --limit 50"
echo "Stop:  gcloud run services delete $SERVICE --region $REGION"
