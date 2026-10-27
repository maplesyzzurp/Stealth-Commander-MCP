# Local Stripe simulation

2026-10-25: `npm test` runs the actual candidate Worker in Miniflare against a Node HTTP server bound to an ephemeral port on `127.0.0.1`. The test transport redirects only `https://api.stripe.com` requests to that server. Any other upstream origin fails. The mock answers undefined endpoints with 501 and does not use real credentials.

The Orbit brief covered Checkout and Portal requests only. This stand-in models the additional customer, account, Price, and subscription endpoints from the official Stripe references linked in the integration contract. A published owner's complete mock was not available in the supplied brief; no mock was uploaded or deployed.

Every dependency call is recorded in the test's in-memory `calls` log without authorization headers. Assertions read that log to prove customer creation, idempotency retries, canonical reconciliation, server-controlled URLs/Price, and Portal customer selection. Supported Stripe paths served during the run; there are no silent simulation members.

Nine tests passed locally. The run exposed and fixed workerd's unsupported `redirect: error` mode; the Worker now uses `manual` and rejects non-2xx responses. A lost customer response, HTML and JSON failures, a 429 response, and a hanging dependency were exercised. The four-second dependency deadline returned unavailable without creating access.

This proves the candidate against the documented stand-in, not a real Stripe account or the hosted website. The tests create and stop their server and Miniflare runtime automatically. No persistent mock data, deployment, or external account setup is needed.

The generated `.wrangler/` directory is excluded by `.gitignore` and the package file allowlist. Automatic approval review rejected its cleanup with “blocked by policy”; do not include that runtime directory in a delivered candidate archive.
