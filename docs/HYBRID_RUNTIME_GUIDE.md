# ALM WebFlow Studio — Hybrid Runtime Guide

## Why WebFlow is hybrid

The hosted Studio is the control plane. It stores governed WebFlow definitions, object metadata, training content, schedules, audit records, provider/Engenie integration, and result/evidence metadata. Browser automation that needs access to company-network applications belongs on an approved workstation-side **WebFlow Local Client**.

This design is intentionally **not** a firewall-bypass mechanism. The Local Client:

- opens no inbound listening port;
- does not create a VPN, reverse proxy, SOCKS proxy, tunnel, or generic remote shell;
- initiates outbound HTTP(S) requests to the Studio;
- accepts only an explicit allow-list of typed WebFlow tasks;
- verifies task signatures before acting;
- runs Playwright/Chromium in the user's existing network context;
- returns WebFlow status, masked recording metadata, screenshots/evidence, and structured results.

## Pairing a client

1. Open **Settings / Runtime → WebFlow Local Clients**.
2. Choose **Create Pairing Token**. The token is one-time and short-lived.
3. On the workstation that will run Chromium:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python local_client.py --studio "https://<studio-route>" --pair "<one-time-token>"
```

4. Save the returned `WEBFLOW_CLIENT_ID` and `WEBFLOW_CLIENT_SECRET` as protected environment variables for subsequent launches:

```bash
python local_client.py --studio "https://<studio-route>"
```

In production, the shared client credential should be stored using the approved workstation/BTP credential mechanism instead of a shell history or plaintext script.

## Trust and signing model

Pairing creates a per-client shared secret. Every Local Client → Studio request includes:

- client ID;
- UTC timestamp;
- HMAC-SHA256 signature over timestamp + HTTP method + path + request-body hash.

The Studio rejects signatures older than five minutes. Studio → Local Client task envelopes are also HMAC-SHA256 signed. Every task carries the authenticated Studio user that requested it (`requested_by`) so execution/recording activity can be tied back to the initiating user and included in the audit trail.

TLS remains mandatory for hosted use. HMAC signing supplements HTTPS; it does not replace it.

The BTP Application Router exposes only the four Local Client transport endpoints (`pair`, `poll`, `heartbeat`, `result`) without an interactive XSUAA login because a background workstation client cannot complete the normal browser login flow. Those routes are still application-authenticated: `pair` requires a short-lived one-time token created by an authenticated WebFlow Admin, and every post-pairing request requires the per-client timestamped HMAC signature. All normal Studio APIs remain behind XSUAA.

## Recording-first workflow

Recording no longer depends on a pre-existing flow.

1. Enter a **Recording Name** and **Starting URL**.
2. Choose the local runtime.
3. Start recording and use the controlled Chromium window normally.
4. Stop recording.
5. Studio persists the recording and creates a Draft WebFlow automatically if the recording was not explicitly associated with an existing flow.

This keeps the user journey in the natural order: **record the process → get a WebFlow → refine/design/govern it**.

## Recorder event-transport fix

The recorder binding now treats browser events as an asynchronous queue. The browser JavaScript binding only enqueues the event and returns immediately; the recorder worker performs screenshots and persistence afterward.

This is important because taking a synchronous Playwright screenshot inside an `expose_binding` callback can stall the browser-to-Python connection after an early click. The queued model also mirrors how remote Local Client events are transported back to hosted Studio.

## Cloud/local responsibility split

| Capability | Hosted Studio / BTP | Local Client |
|---|---:|---:|
| Flow library / Designer | ✓ | |
| Object repository | ✓ | capture source |
| Training replay / distribution | ✓ | |
| Engenie / AI provider orchestration | ✓ | |
| Governance / audit / user attribution | ✓ | signed caller |
| Scheduling / dispatch | ✓ | task consumer |
| Interactive browser recording | | ✓ |
| Company-network browser execution | dispatch/results | ✓ |
| Browser screenshots/evidence | store/preview | capture |
| Arbitrary network proxy/tunnel | **Never** | **Never** |

## Current implementation boundary

This update makes the Local Client transport operational for **recording, direct browser execution, queued execution, scheduled execution, and cancellation**. Every dispatched browser task carries the authenticated initiating user (or the schedule's stored creator identity) and a signed typed task envelope.

Data-driven Excel batches and Performance Studio still use their original local execution managers. In a hosted BTP runtime those two browser-heavy entry points are deliberately blocked until their orchestration is moved onto the same Local Client contract. They are not allowed to silently fall back to cloud Chromium.

Do not implement generic command execution as a shortcut; future client capabilities must remain typed and WebFlow-specific.
