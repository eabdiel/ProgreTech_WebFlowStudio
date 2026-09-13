# ALM WebFlow Studio — Phase 14 Hardening Report

Phase 14 closes the original 0–14 delivery roadmap with pilot-oriented controls around the existing WebFlow runtime. The normalized flow model, recorder, Designer, Object Repository, execution engine, data engine, script export/import, performance engine, scheduler, BTP adapter, Engenie bridge, and AI provider adapters remain the same core architecture.

## Controls implemented

- **Role gates:** Viewer / Runner / Admin role hierarchy is enforced on API mutations. Standalone development receives Admin + Runner roles; BTP sessions consume the XSUAA scopes surfaced by the SAP application-router authentication boundary.
- **Domain governance:** configurable host allow-list supports exact names and wildcard patterns. Recording start and execution paths validate target domains when enforcement is enabled.
- **Destructive-action governance:** recorded/designer actions are inspected for state-changing actions. Direct runs, queue submission, data batches, schedules, retries, Engenie, and external-AI-triggered jobs require explicit confirmation when classified destructive.
- **Performance safety:** destructive flows are blocked from synthetic performance testing by default to avoid accidental repeated state changes.
- **Resource bounds:** timeout, retry, upload size, performance run count/concurrency, and worker concurrency are bounded centrally.
- **Audit trail:** all mutation API responses generate redacted append-only events. Events are SHA-256 hash chained so accidental/tampered edits can be detected by `/api/audit/verify`.
- **Retention:** run/performance/batch artifact cleanup is policy driven. The audit trail remains append-only inside the app; BTP Object Store lifecycle policies should handle archival/deletion.
- **Pilot readiness:** `/api/pilot/readiness` reports the current hardening gates separately from the Phase 11 infrastructure readiness endpoint.
- **Object Repository cleanup:** `capture_count` is now recalculated from saved recordings during rebuild rather than increasing every time the repository is rebuilt.

## Deliberate boundaries

1. **Browser automation is intentionally local-client-only in hosted BTP.** Recording and browser execution stay inside the approved company network. BTP is the control plane. No VPN, proxy, tunnel, port-forward, remote-shell, or firewall-bypass capability is part of WebFlow.
2. **The current BTP persistence bridge is single-active-instance.** Multi-instance active/active locking and distributed queue semantics remain a future production-scale enhancement.
3. **Code-managed uploaded Python is validated/stored, not automatically executed in Studio.** Full arbitrary-code execution should use an isolated worker/container if added later.
4. **Chromium is the supported pilot browser.** Firefox/WebKit are future coverage targets if business applications require them.
5. **AI providers never own approval.** Provider-generated `confirmed=true` is ignored unless explicit caller confirmation reaches the governed WebFlow layer.

## Pilot acceptance sequence

1. Run `python main.py` locally and confirm `/api/health` reports `14/14`.
2. Record a non-destructive flow and execute it headed and headless.
3. Enable the domain allow-list and confirm an out-of-policy domain is blocked.
4. Record or mark a destructive flow and confirm Run / Queue / Schedule request explicit approval.
5. Confirm Performance Studio refuses destructive flows.
6. Upload a small CSV/XLSX and execute a data-driven non-destructive flow.
7. Queue two non-destructive jobs and verify worker concurrency remains bounded.
8. Perform several mutations, open Settings → Audit Trail, and verify the audit hash chain.
9. Run retention cleanup and confirm only expired artifacts are removed.
10. On BTP, verify XSUAA roles, persistent Object Store, Local Client pairing/heartbeat/signature validation, route/authentication, Hub registration, and Engenie metadata before enabling the pilot.


## Post-roadmap Hybrid Runtime v2

The hosted security boundary was tightened after Phase 14: direct recording, direct execution, queued execution, scheduled execution, and cancellation can be dispatched only to an explicitly paired Local Client when WebFlow is hosted on BTP. Data-driven batch execution and Performance Studio are deliberately blocked in hosted mode until they are migrated to the same typed Local Client task contract.

Local Client transport is outbound-only. Each task is typed and allow-listed, includes the initiating Studio identity, and is HMAC signed. The client exposes no inbound listener and no generic command/shell/tunnel primitive.

The current prototype persists the per-client shared HMAC secret in WebFlow's server-side persistence. This is suitable for controlled development/pilot validation but should be replaced by an approved credential/secret store before broad production use.
