# ProgreTech WebFlow Studio — browser automation with Playwright

Browser automation studio with Playwright: record workflows, edit and run flows, capture screenshots, and use an optional local client.

A project of **[ProgreTech LLC](https://progretech.com)**, owned and maintained by **Ed Rodriguez**. Third-party components and contributions retain their respective ownership and notices.

[Project website](https://progretech.com) · [Report an issue](https://github.com/eabdiel/ProgreTech_WebFlowStudio/issues) · [Contribute](CONTRIBUTING.md)

Standalone browser workflow authoring and execution studio, from the ALM WebFlow Studio family.

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

## Collaboration

Reproducible bug reports, platform compatibility, installation documentation, and small regression fixes are useful ways to help. Read [CONTRIBUTING.md](CONTRIBUTING.md) for issue reports, proposed changes, and attribution requirements.

## License and reuse

This repository uses custom ProgreTech source-available terms; see [license.md](license.md). Read the permitted uses, attribution, and contribution terms before reusing or submitting code. Public visibility is not an OSI-approved open-source license.

## More from ProgreTech

Explore [CodeSeal](https://codeseal.progretech.com) for signed software provenance and project history.

Discover the wider portfolio at [progretech.com](https://progretech.com). These links identify related products; they do not imply a bundled integration or shared license.
