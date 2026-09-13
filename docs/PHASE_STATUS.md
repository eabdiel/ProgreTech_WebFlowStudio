# ALM WebFlow Studio — Phase Status

**Current:** Phase 14 of 14 — Hardening & Pilot  
**Status:** Delivered — original roadmap complete  
**Next:** Validation, adjustment backlog, and controlled pilot

## Completed roadmap

- Phase 0 — UX prototype & requirements lock
- Phase 1 — Standalone application skeleton
- Phase 2 — Controlled browser + recorder MVP
- Phase 3 — Object Repository & resilient selectors
- Phase 4 — Replay / training mode
- Phase 5 — Executable automation engine
- Phase 6 — Excel / CSV data-driven execution
- Phase 7 — Visual logic + governed Python
- Phase 8 — Script export / import
- Phase 9 — Performance & synthetic monitoring
- Phase 10 — Scheduling / worker model
- Phase 11 — ALM Hub / SAP BTP plug-in
- Phase 12 — ALM Engenie integration
- Phase 13 — External AI provider adapters
- Phase 14 — Hardening & pilot

## Phase 14 completion criteria

Security/governance controls, audit trail, destructive-action confirmation, domain policy, bounded runtime resources, retention controls, BTP/pilot readiness checks, pilot documentation, and final regression validation are now represented in the baseline. Remaining work should be treated as pilot feedback/enhancement work rather than another roadmap phase.


## Post-roadmap enhancement — Hybrid Runtime v2

**Status:** Delivered for recording, direct execution, queue execution, scheduled execution, and cancellation.

- BTP/Hub is the control plane; Chromium browser work stays on an outbound-only Local Client inside the approved network.
- Recording is now recording-first: no pre-existing Flow is required. Stopping a new recording creates a Draft WebFlow automatically.
- Recorder browser events are queued out of the Playwright binding callback before screenshots/persistence, avoiding the synchronous callback re-entry that could stall event capture.
- Local Clients are paired with short-lived one-time tokens and then use timestamped HMAC requests/tasks. Dispatched tasks retain the authenticated initiating user.
- No VPN, proxy, tunnel, port-forward, generic shell, or firewall-bypass behavior is provided.
- Hosted Data-driven batches and Performance Studio remain blocked until their browser execution is moved onto the Local Client contract.
