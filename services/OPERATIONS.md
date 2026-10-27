# Private services operations and acceptance

These sources are a reviewable private service bundle. No command in this document authorizes deployment, billing activation, account changes or a production recovery. Use the coordinator's explicit authorization and a synthetic staging account for acceptance.

## Components and defaults

| Component | State and boundary |
| --- | --- |
| oauth-gateway | Browser/OAuth routing; mandatory private authority and abuse-limit bindings |
| strong-auth-service | One private SQLite Durable Object named account-authority; account/OAuth CAS authority |
| rate-limit-service | Private platform limiter adapter; account-unique namespace ID must be configured |
| billing-service | Private test-only Stripe ledger; disabled by default; accessAllowed always false |

Do not expose the three private service Workers through workers.dev, previews, custom domains or zone routes. Source example configs disable those routes. Keep PRIVATE_MODE=true and the approved-account allowlist explicit. Secrets, namespace IDs, deployment configs, logs and generated bundles stay outside git. services/.gitignore covers dependencies and generated build/runtime caches; it is not a secret scanner.

## Reproducible local checks

Install the gateway's lockfile dependencies using npm ci --ignore-scripts. From services/oauth-gateway run npm test. Then run npm test in services/strong-auth-service, services/rate-limit-service and services/billing-service. Sibling test harnesses resolve the same pinned gateway dependencies.

Tests exercise synthetic users, paired devices, OAuth clients and payment fixtures. They need functioning local workerd/Miniflare loopback sockets. A setup fetch failure is not an application assertion and must be resolved before claiming a green full suite. Never remove failing tests from the command or convert setup failures to skips to produce a release result. Save test output outside the checkout with a timestamp, Node/dependency versions and source commit.

Before a release, scan the staged diff for personal paths, account identifiers, secrets, captures and deployment state; confirm the ninja PNG hash against the approved source. Review each compiled bundle hash against the exact checked source. Build outputs are generated and should not be committed.

## Private end-to-end acceptance

Using the exact proposed source commit and approved synthetic account:

1. Verify discovery, sign-in, browser-bound consent and explicit PC selection through the intended real OAuth client.
2. Confirm ordinary tools, a real bounded image and a long command through the complete client/gateway/PC path. Inspect the image pixels and its unchanged provenance; measure actual performance instead of promising a frame rate.
3. Verify cross-account denial, selected-PC behavior, refresh/reconnect and JSON protocol handling. Check that start_tunnel is neither listed nor remotely executable.
4. While retaining a test connection, unlink/re-pair/rotate/revoke/logout in separate cases and verify old grants, refresh/device credentials and pending tickets stay rejected. Stop only the test-owned sessions/tunnel.
5. Inspect canonical-origin/private asset restrictions and anonymous responses. Verify private services have no public route. Do not disable authentication or private mode to make a test succeed.
6. If test billing is explicitly enabled, use test credentials and a test price only; verify webhook signatures/idempotency and the invariant accessAllowed=false. A checkout redirect is not proof of paid access.

Historical test files or earlier live reports are not fresh acceptance for a new commit.

## Rollback and fail-closed operation

Record a known-good source commit, gateway/PC contract versions and private binding map before an authorized deployment. A failed authority/limiter/portal request must deny access. During an incident, preserve state and sanitized evidence; do not bypass the authority or re-enable a retained capability URL.

Rollback application code only to a reviewed version compatible with the current strong authority and ticket contract. Do not point authorization at the former eventually consistent KV store, recreate old grants, or restore revoked records merely to regain availability. Test the rollback against synthetic revoked sessions before production use. A production rollback remains an operator action, not an automated effect of this repository.

## Recovery proof still required

This checkout has migration/conflict/expiry/replay tests but no production backup-and-restore attestation. It supplies no unverified export/restore command or disaster-recovery guarantee. Before public service, the operator must document platform-supported backups, encryption/access controls, retention/deletion, authority and billing-ledger consistency, and a recovery point/time objective. Rehearse recovery into an isolated environment containing only synthetic records and verify revoked credentials remain revoked, ownership claims remain unique, billing identities stay separate and no live route points at the rehearsal.

Record the rehearsal source version, protected snapshot reference, restore steps, timing and security checks. Never commit a real snapshot, credential, account response or raw capture. Do not claim recovery readiness until that rehearsal passes. Current single-object throughput, service outages, platform per-location rate-limit behavior and production restore timing also need measured operational acceptance.
