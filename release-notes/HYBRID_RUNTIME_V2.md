# Hybrid Runtime v2

- Fixed recorder event delivery by continuously pumping the synchronous Playwright event loop while the headed browser is open.
- Browser close is treated as a normal recording finish.
- Stop is idempotent and cannot remain indefinitely in `stopping`.
- Added scoped cleanup for Chromium descendants owned by the active recorder session.
- Added user-friendly Local Client distribution from Studio.
- Studio serves a ZIP containing the stable signed EXE (when present in `client-dist/`) plus a one-time bootstrap file.
- Local Client consumes the bootstrap, saves its paired identity in the current user's profile, and no longer requires manual token/environment-variable setup for normal users.
- Manual pairing remains under Advanced settings for support/development.
