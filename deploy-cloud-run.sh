#!/usr/bin/env bash
set -euo pipefail
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-alm-webflow-studio}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:latest"
gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID"
EXTRA=()
[[ -n "${WEBFLOW_GCS_BUCKET:-}" ]] && EXTRA+=(--set-env-vars "WEBFLOW_GCS_BUCKET=${WEBFLOW_GCS_BUCKET}")
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --platform managed --port 8080 --memory 2Gi --cpu 2 --concurrency 4 --max-instances 1 "${EXTRA[@]}" --project "$PROJECT_ID"
