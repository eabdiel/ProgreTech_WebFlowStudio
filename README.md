# ALM WebFlow Studio — Google Cloud Run baseline

Standalone - 

## Included
- Latest Hybrid Runtime v5 authoring/execution UI and fixes.
- Latest screenshot Fit/scroll behavior and short-lived evidence retention.
- Cloud Run runtime detection and PORT/0.0.0.0 binding.
- Playwright/Chromium container image.
- Optional Google Cloud Storage mirror for persistence.
- Optional Local Client for workstation-only targets.

## Local
```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```
Open http://127.0.0.1:5678.

## Cloud Run
Recommended: create a GCS bucket, grant the Cloud Run service account object access, then:
```bash
export PROJECT_ID=your-project
export WEBFLOW_GCS_BUCKET=your-bucket
./deploy-cloud-run.sh
```
Set `WEBFLOW_PUBLIC_URL` after deployment if you want the canonical route reflected in readiness/client bundles.

The supplied deployment uses 2 CPU, 2 GiB RAM, concurrency 4, and max instances 1. Max instances is intentionally 1 because the current persistence layer mirrors local JSON/files to GCS rather than using a transactional database.

## Evidence retention
Recording screenshots default to 24 hours and may be explicitly saved for 3/7/14/30 days. Execution screenshots remain 24 hours. Reporting metadata remains persistent. Use **Download Local Copy** for indefinite recording retention.
