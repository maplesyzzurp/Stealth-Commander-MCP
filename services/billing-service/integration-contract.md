# Private billing candidate

This checkout includes the private test-only service and its authenticated website adapter. It is disabled by default and every status response has accessAllowed: false; it cannot grant paid PC access. Pricing, currency, product terms and usage limits remain undecided. Source tests do not establish production deployment or recovery readiness.

## Private service interface

The gateway calls a private Cloudflare service binding at `https://billing.internal`. Do not publish these routes, forward arbitrary requests to this service, or treat the hostname/header as authentication. The security boundary is the private binding, accessible only to the trusted gateway.

| Method and path | Headers and body | Result |
| --- | --- | --- |
| `POST /createcheckout` | `Content-Type: application/json`, `X-Stealth-Account-Id: <verified stable account ID>`, body `{}` | Test Checkout URL |
| `POST /createportal` | Same | Test customer Portal URL; 404 without a bound customer |
| `POST /status` | Same | Canonical test subscription status; access is always denied |
| `POST /webhook` | Original `Stripe-Signature`, JSON content type, exact raw Stripe body | Verified event acknowledgement |

Browser-supplied customer IDs, subscription IDs, prices, account IDs, return URLs, and all other body fields are rejected. Requests with an `Origin` header are rejected; the service emits no CORS permissions. Browser routes live in the authenticated website gateway.

## Website gateway adapter

`../oauth-gateway/src/billing.js` exports `handleBilling(request, env, context)`. Its caller supplies either `null` or `{ user, session }` after verifying the current `sc_session` against account authority and the deployment's private-account allowlist. The adapter also requires exactly one matching session cookie. A bearer credential or browser account header cannot select the billing account.

The gateway needs the private service binding `BILLING_SERVICE`, the existing `AUTH_RATE_LIMITER`, `BILLING_TEST_ENABLED="true"`, `BILLING_MODE="test"`, and a separate secret `BILLING_CSRF_SECRET` of at least 32 random characters. The service and gateway both reject other billing modes. No Stripe credential belongs in the gateway or browser.

| Website route | Request | Successful JSON response |
| --- | --- | --- |
| `GET /api/billing/status` | Current session cookie | `{ mode: "test", billingStatus: "none" \| "active" \| "pending_or_unpaid", testSubscriptionActive: boolean, accessAllowed: false, csrfToken: string }` |
| `POST /api/billing/checkout` | Current session cookie, `X-CSRF-Token`, JSON `{}` | `{ mode: "test", url: "https://checkout.stripe.com/...", accessAllowed: false }` |
| `POST /api/billing/portal` | Current session cookie, `X-CSRF-Token`, JSON `{}` | `{ mode: "test", url: "https://billing.stripe.com/...", accessAllowed: false }` |
| `POST /api/billing/webhook` | Original `Stripe-Signature` and raw JSON body | `{ received: true }` with optional `duplicate` or `ignored` |

Browser calls require `Sec-Fetch-Site: same-origin`; `Sec-Fetch-Mode`, when present, must be `cors` or `same-origin`. POST calls require `Origin` to equal `GATEWAY_ORIGIN` exactly. GET permits an absent `Origin`, as normal browser fetches omit it, and rejects any different origin. POST bodies have a 2 KiB cap and accept only an empty JSON object. Query parameters are rejected. Status and actions use separate per-source and per-account limiter keys. Missing, malformed, or unavailable abuse protection denies the request.

The account key is an opaque SHA-256 hash of a version label and immutable `user.id`, when present, or the current canonical email and canonical ISO `created_at`. Keep those profile fields immutable. Adding `user.id` later requires a billing-account migration because it changes the key. API key rotation does not change the billing key. The CSRF token is an HMAC bound to the website origin, billing key, and verified session, so changing account or session invalidates it.

Only the webhook route bypasses browser session and CSRF checks. The adapter preserves its body bytes, caps the body at 256 KiB, strips caller identity headers, and forwards only to internal `/webhook`. The billing worker verifies the Stripe signature before processing. All website responses use `Cache-Control: no-store`; malformed service replies and unexpected Stripe-hosted URL origins fail closed.

Gateway integration checklist (implemented adapter behavior must remain covered by tests):

1. Fix and regression-test the account rotation, stale device mutation, and legacy-proxy revocation findings first.
2. Add same-origin browser endpoints for Checkout, Portal, and status. Resolve the current session against strong authority on every call. For POST actions, require an exact allowed `Origin`, reject cross-site requests, and validate a session-bound CSRF token. Apply per-account and source abuse limits. Never accept an account identity from browser JSON, query parameters, cookies other than the validated session, or forwarded headers.
3. Construct a new internal request with only the content type and gateway-derived `X-Stealth-Account-Id`. Strip browser `X-Stealth-*` headers rather than forwarding them. Use an immutable account identifier. The present profile can supply a derived opaque ID such as SHA-256 of `[user.email, user.created_at]`, provided both values come from the current authenticated profile, creation time is valid and immutable, and registration replacement races are fixed. Do not bind billing to an email that can later be reassigned to another account.
4. Add one explicit public Stripe webhook adapter route only when deployment is authorized. Preserve the raw body bytes and original signature, enforce the 256 KiB body cap, and forward only to internal `/webhook`. This route uses Stripe signatures rather than browser CSRF. Do not expose any other internal route through it.
5. Add website billing controls that open the returned Stripe-hosted URL. The `/billing?checkout=returned` page must fetch status; the redirect itself proves nothing about payment. Do not add purchase or upsell tools to MCP.

## State and Stripe behavior

One SQLite-backed Durable Object owns billing records independently of account profile snapshots. Its unique customer column and serialized operations prevent competing requests from binding one customer to multiple accounts. A creation intent is saved before contacting Stripe. Retry uses the same idempotency key; after 23 hours an unresolved intent requires operator reconciliation rather than risking a new customer after Stripe prunes its idempotency key. Checkout sessions also have durable intents, use a one-hour expiry, and reuse an existing open session. An ongoing subscription directs the user to Portal instead of creating another subscription.

The service checks the expected Stripe account and test recurring Price, requires test API credentials, and rejects live objects. Customer ownership comes from the stored mapping plus a server-generated binding marker on the canonical Stripe Customer. Browser or event metadata cannot assign ownership.

Webhooks verify HMAC-SHA256 over the original body and signed timestamp, with a five-minute tolerance. Event IDs are deduplicated for 31 days; old entries are pruned when another supported event arrives. Reprocessing after pruning remains safe because events trigger a fresh customer/subscription lookup rather than applying the event snapshot. Unknown customer IDs cannot establish a mapping. Connect/organization events and different API versions are rejected. Failed reconciliation returns a non-2xx response so Stripe can retry.

Configure snapshot webhooks for the eight event types listed in `supportedEvents` in `worker.js`, using API version `2026-09-30.endive`. Test secrets belong in secret bindings named `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`, never in source or this example configuration. A Stripe CLI login credential is not automatically a suitable long-lived service secret. Portal must have an appropriate test configuration in Stripe or session creation returns unavailable. Set the optional server-controlled `STRIPE_PORTAL_CONFIGURATION_ID` to a dedicated `bpc_...` configuration to avoid changing a shared account's default. If omitted, Stripe uses its default configuration. The browser cannot select it.

## Validation and activation limits

Run `npm test` in this directory. It reuses the installed Miniflare dependency from `../oauth-gateway`; no new package installation is needed. The worker runs in workerd and sends real HTTP requests to a loopback-only Stripe stand-in. Nine tests cover customer/session concurrency, account separation, rejected browser fields, disabled/live configuration, lost-response retries across eviction, Portal ownership, raw signature validation, replay, out-of-order events, mismatched canonical customers, and upstream errors/timeouts. Run `node --test test/billing.test.mjs` in `../oauth-gateway` for 14 gateway checks. These include a workerd gateway-to-ledger service-binding test that creates a mock Checkout session, rejects another account's Portal request, verifies a raw signed event, and rejects a changed body. The local tests create no real Stripe objects.

`sandbox-runtime.mjs` exports an optional helper for an explicitly authorized real Stripe sandbox smoke test. It is never imported by `npm test` and does nothing when run by itself. The caller supplies test bindings in memory, provisions disposable test resources separately, and must clean up the returned test object IDs. Its `observed` records omit authorization headers, payment data, and hosted URLs. Only the coordinating operator should invoke this helper; it makes real test-account mutations when its `call()` method is used.

An independent run on 2026-10-25 passed all nine local tests. A separate real Stripe sandbox smoke test also created Checkout and Portal sessions successfully, denied a different synthetic account's Portal request, and confirmed that no payment, subscription, or PC entitlement was created. The evidence is in `../commercial-setup/stripe-sandbox-smoke.json`. The US$1/month fixture was disposable test data, not launch pricing. The Checkout session was expired, the synthetic customer deleted, and the Price and Product archived. The dedicated test Portal configuration remains active because Stripe made this account's first configuration the default and rejects deactivating its default configuration. No live Stripe configuration was changed.

Before activation, fix the gateway findings, settle product terms and any pricing needed for the intended mode, verify domain ownership and deployment privacy, choose an explicit test Price, configure a test webhook, and run authorized sandbox payment/cancellation flows through the website. Real webhook delivery, payment completion, cancellation, DNS, TLS, and live billing remain unverified. Production mode is intentionally unsupported; enabling paid access requires a separately reviewed change. The single-object synchronous reconciliation is suitable for this test candidate; production traffic and webhook queueing need capacity review. No quotas or metered billing are implemented.

Primary references used to fill the Orbit request brief's gaps: [customer creation](https://docs.stripe.com/api/customers/create), [customer retrieval](https://docs.stripe.com/api/customers/retrieve), [idempotent requests](https://docs.stripe.com/api/idempotent_requests), [subscription listing](https://docs.stripe.com/api/subscriptions/list), [webhook verification and delivery](https://docs.stripe.com/webhooks), [Checkout](https://docs.stripe.com/api/checkout/sessions/create), [Portal](https://docs.stripe.com/api/customer_portal/sessions/create), and [API versioning](https://docs.stripe.com/api/versioning).
