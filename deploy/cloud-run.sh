#!/usr/bin/env bash
set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to your GCP project ID}"
: "${API_TOKEN_SECRET:?Set API_TOKEN_SECRET to an existing Secret Manager secret name containing the operator token}"
REGION="${REGION:-us-central1}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
PROJECT="$GOOGLE_CLOUD_PROJECT"
ACCOUNT="micro-grid-runtime@$PROJECT.iam.gserviceaccount.com"
TAG="$(date -u +%Y%m%d%H%M%S)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SERVICES=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
  firestore.googleapis.com aiplatform.googleapis.com secretmanager.googleapis.com)
[[ -z "${BIGQUERY_TABLE:-}" ]] || SERVICES+=(bigquery.googleapis.com)
[[ -z "${GCS_BUCKET:-}" ]] || SERVICES+=(storage.googleapis.com vision.googleapis.com)
[[ -z "${PUBSUB_TOPIC:-}" ]] || SERVICES+=(pubsub.googleapis.com)
[[ -z "${EARTH_ENGINE_PROJECT:-}" ]] || SERVICES+=(earthengine.googleapis.com)
gcloud services enable "${SERVICES[@]}" --project="$PROJECT"
gcloud secrets describe "$API_TOKEN_SECRET" --project="$PROJECT" >/dev/null

if ! gcloud artifacts repositories describe micro-grid --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create micro-grid --repository-format=docker --location="$REGION" --project="$PROJECT"
fi
if ! gcloud firestore databases describe --database='(default)' --project="$PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' --location="$FIRESTORE_LOCATION" --type=firestore-native --project="$PROJECT"
fi
if ! gcloud iam service-accounts describe "$ACCOUNT" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create micro-grid-runtime --display-name="Micro-grid simulator runtime" --project="$PROJECT"
fi
for role in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$ACCOUNT" --role="$role" --condition=None >/dev/null
done
gcloud secrets add-iam-policy-binding "$API_TOKEN_SECRET" --project="$PROJECT" \
  --member="serviceAccount:$ACCOUNT" --role=roles/secretmanager.secretAccessor >/dev/null

gcloud builds submit "$ROOT" --project="$PROJECT" --config="$ROOT/deploy/cloudbuild.yaml" \
  --substitutions="_REGION=$REGION,_TAG=$TAG"
gcloud run deploy micro-grid-backend --project="$PROJECT" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT/micro-grid/backend:$TAG" \
  --service-account="$ACCOUNT" --port=8000 --allow-unauthenticated \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=3 --concurrency=8 --timeout=120 \
  --set-env-vars="BACKEND_STORE=firestore,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=$GEMINI_MODEL,BACKEND_PRODUCTION=true,BIGQUERY_TABLE=${BIGQUERY_TABLE:-},GCS_BUCKET=${GCS_BUCKET:-},PUBSUB_TOPIC=${PUBSUB_TOPIC:-},EARTH_ENGINE_PROJECT=${EARTH_ENGINE_PROJECT:-}" \
  --set-secrets="BACKEND_API_TOKEN=$API_TOKEN_SECRET:latest"
BACKEND_URL="$(gcloud run services describe micro-grid-backend --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
gcloud run deploy micro-grid-dashboard --project="$PROJECT" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT/micro-grid/frontend:$TAG" \
  --port=8080 --allow-unauthenticated --cpu=1 --memory=256Mi \
  --min-instances=0 --max-instances=3 --concurrency=80 --timeout=120 \
  --set-env-vars="BACKEND_URL=$BACKEND_URL"
gcloud run services describe micro-grid-dashboard --project="$PROJECT" --region="$REGION" --format='value(status.url)'
