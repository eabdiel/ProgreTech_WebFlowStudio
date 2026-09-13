# Local Client distribution folder

Drop the enterprise-built and code-signed `WebFlowLocalClient.exe` in this folder.

The Studio does **not** modify or re-sign the EXE per user. Instead, each download is a ZIP containing the same signed executable plus a short-lived `webflow-client.bootstrap.json`. On first launch the client consumes the one-time token, pairs with the Studio, stores its client identity in the current user's profile, and deletes the bootstrap file.

This keeps executable signing/reputation stable and avoids embedding long-lived credentials in binaries.
