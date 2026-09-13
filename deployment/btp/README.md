# SAP BTP / ALM Hub deployment notes — Phase 11

Phase 11 preserves the standalone application and adds a Cloud Foundry/Hub runtime adapter around it.

## Runtime shape

- `alm-webflow-studio-router`: SAP Application Router + XSUAA trust boundary.
- `alm-webflow-studio-api`: the existing Python WebFlow application and worker/scheduler runtime.
- `alm-webflow-objectstore`: an existing SAP Object Store/S3-compatible service used by the Phase 11 persistence mirror.
- The backend remains at **one active instance** in Phase 11 because the compatibility mirror is filesystem-first. Multi-instance distributed persistence/locking is a Phase 14 hardening concern.

## Before deployment

1. Create or identify an Object Store service instance named `alm-webflow-objectstore`, or update `mta.yaml` / `manifest.yml` to your actual service name.
2. Ensure Chromium is available in the Cloud Foundry runtime. The Python package alone is not enough; the Playwright browser binary and its OS libraries must also be present. For the first cloud pilot, validate this with `/api/btp/readiness` before enabling scheduled execution.
3. Build/deploy with your normal MTA pipeline. The included MTA creates XSUAA and the approuter but treats Object Store as an existing service.
4. Register the approuter route in the ALM Engineering Hub using `/api/hub/plugin` or `config/hub-plugin.json` as the metadata contract.
5. Map the XSUAA role templates to the appropriate IAS/IdP groups.

## Important Phase 11 boundary

Cloud **execution/scheduling** can run in the backend once Playwright/Chromium is available. The existing local Recorder opens a visible browser window, so a truly interactive cloud recording experience still needs a browser-streaming/remote-control surface. Phase 11 does not pretend a headed Chromium window in Cloud Foundry is visible on the user's desktop. Recordings can still be authored locally and deployed/synchronized for cloud execution.

## Readiness endpoints

- `/api/health`
- `/api/runtime`
- `/api/btp/readiness`
- `/api/session`
- `/api/hub/plugin`
- `/api/engenie/manifest`
- `/api/engenie/tutorial`

The Object Store mirror hydrates `data/` at startup and periodically uploads changed state/artifacts. Credentials are read from the service binding or `WEBFLOW_S3_*` environment variables and are never returned by the runtime endpoint.
