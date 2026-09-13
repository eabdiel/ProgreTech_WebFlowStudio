# WebFlow Local Client distribution

## Recommended end-user model

WebFlow Studio is the control plane. Browser recording and execution remain on an approved workstation or automation VM inside the corporate network. The Local Client is outbound-only and polls Studio for typed, signed WebFlow tasks. It is not a tunnel, proxy, VPN, remote shell, or firewall-bypass mechanism.

For non-technical users, place an enterprise-built/code-signed `WebFlowLocalClient.exe` under `client-dist/`. The Settings page then serves a ready-to-pair ZIP. The ZIP contains the same signed EXE for every user and a unique short-lived `webflow-client.bootstrap.json`. The user extracts the ZIP and double-clicks the EXE. No manual token copy/paste or command line is required.

## Why the token is not compiled into the EXE

Do not generate a new EXE per user and do not bake a long-lived client secret into the executable. That complicates enterprise code signing, malware/reputation scanning, rotation, patching, and support, while still not making the secret impossible to extract. A stable signed EXE plus a one-time bootstrap file is simpler and safer.

## First launch

1. Studio creates a pairing token valid for 30 minutes.
2. Studio packages the signed EXE plus bootstrap JSON.
3. Client reads the adjacent bootstrap file.
4. Client performs one outbound HTTPS pairing request.
5. Studio returns the per-client HMAC identity.
6. Client stores the identity in the current user's local profile and deletes the bootstrap file.
7. Future launches require no command-line flags.

## Packaging the EXE

A practical Windows build can be produced with PyInstaller or the organization's approved packaging system. Ensure Playwright Chromium is installed/bundled according to your desktop software policy. Once the executable is signed, copy it to `client-dist/WebFlowLocalClient.exe`; no WebFlow code change is required.
