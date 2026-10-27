# Stealth Commander OAuth gateway

This source serves the authenticated dashboard and universal OAuth endpoint at /mcp. It forwards requests to one explicitly authorized paired PC. The PC must support the same single-use ticket contract as this gateway. Hidden desktops separate GUI input; they do not sandbox the Windows account.

Use Node.js 22 or newer. Install exact dependencies from package-lock.json:

```powershell
cd services/oauth-gateway
npm ci --ignore-scripts
npm test
```

The default test command builds the real worker and includes every test/*.test.mjs file, including public tool declarations and legacy credential migration. Workerd/Miniflare tests require local loopback sockets. Worker outbound traffic is replaced by synthetic fixtures; the billing fixture uses a loopback Stripe simulation. Tests do not charge a card or operate a real PC.

The sibling strong-auth-service, rate-limit-service and billing-service tests resolve Miniflare from this package's pinned dependency installation. Run npm test in each sibling after installing this package.

## Private configuration

wrangler.example.jsonc and pages-bindings.example.jsonc are incomplete examples, not deployment instructions or live account settings. Keep PRIVATE_MODE=true. A missing, empty or malformed PRIVATE_ACCOUNT_ALLOWLIST denies accounts; configure only approved account emails outside the source repository. Configure fresh CONTROL_CSRF_SECRET through the platform secret manager, never a committed variable. Do the same for BILLING_CSRF_SECRET only if enabling the test-only billing UI.

AUTH_STORE, AUTH_RATE_LIMITER and REGISTRATION_RATE_LIMITER must be private service bindings. OAuth/account state is stored through the strong authority, not a direct gateway KV binding. Complete private service namespace bindings and migration configuration using reviewed account-owned resources. Build assets from this checkout; the supplied icon is the approved ninja image.

BILLING_TEST_ENABLED defaults false and BILLING_MODE remains test. The billing worker accepts test credentials only and always returns accessAllowed=false. No production payment/access entitlement is implemented by this test service.

## Public directory domain verification

When the submission portal provides a domain challenge, configure its exact token as OPENAI_APPS_CHALLENGE in the gateway deployment settings, outside the repository and plugin archive. The gateway serves that single token as plain text at /.well-known/openai-apps-challenge for GET and HEAD requests, including when PRIVATE_MODE=true. Missing or malformed tokens return 404; no other route becomes public. The handler does not change account access, OAuth consent, PC pairing, or the private dashboard. Keep a challenge already used by another plugin intact; use the portal's permitted alternate host or parent domain instead of replacing it.

The product landing page at /, the informational /about and /support pages, and the original /icon.png are independently public for GET and HEAD requests. The landing page maps to assets/landing.html while the existing assets/index.html remains the private control center. The pages use Cloudflare's default clean-URL handling; raw .html aliases, trailing-slash variants, and other assets remain protected in private mode. Do not configure SPA fallback or force trailing slashes for these assets. These informational pages do not receive account credentials, run scripts, submit forms, or expose the dashboard. Deploy all page files and the route handler together.

Account sign-in is at /sign-in and sends credentials only to the existing same-origin /api/auth/login endpoint. Successful sign-in opens /dashboard, which requires a current allowed account session before serving the private control center. Credential rejection stays generic; throttling, temporary service failure, and network failure have separate guidance without displaying server response bodies. Public information and the sign-in form remain readable during account-store outages, while account, dashboard, OAuth, and PC routes continue to fail closed. PRIVATE_ACCOUNT_ALLOWLIST is the account approval setting; PRIVATE_ALLOWED_EMAILS is not supported.

## Legacy client migration

Universal OAuth /mcp remains the preferred external connection. Account sign-in, pairing, owner sessions and Authorization-header compatibility remain available; this change does not rotate credentials or alter the production deployment.

| Client request | Default behavior |
| --- | --- |
| POST /mcp with OAuth token | Selected PC from explicit consent |
| POST /api/mcp with Authorization: Bearer credential and X-Stealth-Device-Id: owned-pc | Authenticates the existing owner credential and uses exactly owned-pc |
| POST /api/mcp without a selected PC | 400 after authentication; no first-PC fallback |
| /api/mcp/credential/owned-pc | 410; credentials in URLs disabled |
| Same URL with LEGACY_MCP_URL_KEYS_ENABLED=true | Temporary compatibility for the explicit owned PC |
| URL/header PC selectors disagree, or extra path segments | 400; no forwarding |

Never put a real credential in documentation, logs, shell history or a support ticket. For an existing client that needs time to migrate, an operator may explicitly set LEGACY_MCP_URL_KEYS_ENABLED=true in its reviewed private configuration, move the client to OAuth or the header-and-selector form, verify that it selects the intended PC, then turn the flag off. Even compatibility mode has no implicit first-PC selection. Previously copied URL credentials should be treated as exposed to the places they were copied; credential rotation is a separate authorized operation.

The owner dashboard's existing account-key functions remain for compatibility. This source change does not claim those keys have been removed from every signed-in owner response. Do not publish account responses, device connection metadata or request URLs in diagnostics.

## Transport and declarations

OAuth tool discovery declares pc:control and preserves tool schemas/custom metadata. Cached capture is read-only; runtime producers/commands are not labeled read-only. Tunnel setup stays local and is omitted/blocked through OAuth. The gateway does not implement or filter programming-language adapters.

POST requests are bounded JSON objects (1 MiB); PC replies are bounded JSON (8 MiB), with a 330-second deadline and no SSE support. 202/204 empty replies are preserved. Image content blocks are passed unchanged. Metadata minimization omits known server credentials and connection URLs but preserves ordinary command content; full PC control can deliberately access data present on that PC.

See ../OPERATIONS.md for validation, rollback and recovery limits.
