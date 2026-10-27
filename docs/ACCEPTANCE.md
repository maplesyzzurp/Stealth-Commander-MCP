# Release acceptance

Run these checks against one frozen source commit and its exact packed artifact.
Record outcomes separately with the tested versions and hashes. This checklist
defines acceptance; its presence does not mean a check has passed.

Keep logs, state, screenshots, and test projects outside the checkout. Use a
dedicated test account and PC for remote authorization checks. Never fall back
to physical input, a personal browser profile, or another user's session.

## Automated checks

From the repository root, using the supported Python interpreter:

~~~powershell
python -B -m unittest discover -s tests -p test_release_contract.py -v
python -B -m unittest discover -s skills/stealth-ghost-delegation/scripts -p 'test_*.py' -v
~~~

The first command tests the real stdio entry point: initialization/version,
tool discovery, flat required fields, complete documentation, command success
and failure, malformed calls, and validation before process or service startup.
Its only real command children print a marker or return a known exit code.
The second command includes loopback servers and mocked ownership/security
cases. Passing either command does not establish fresh GUI or OAuth behavior.

Install the gateway's locked dependencies in services/oauth-gateway:

~~~powershell
npm ci
npm test
~~~

Then run npm test in services/strong-auth-service and services/rate-limit-service.
These services share the gateway test runtime; they do not carry separate lockfiles.
A workerd/network setup failure is an environment-blocked check, not a pass
and not proof that every affected authorization case is a code defect.
Preserve PRIVATE_MODE and production authentication restrictions.

## Live Windows checks

Run the families sequentially to keep resource usage bounded. Use a new writable
directory for each run, and inspect both the result and cleanup evidence.

~~~powershell
$Evidence = Join-Path $env:TEMP ('stealth-acceptance-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Evidence | Out-Null
python -B tests/live_native_capture.py --work-dir $Evidence --output (Join-Path $Evidence 'native-capture.json')
~~~

This opt-in test launches only the included neutral Qt fixture on an owned
inactive desktop. It starts an observer for that native session, starts its own
dashboard on an ephemeral loopback port, requests an exact published PNG through
MCP, and checks the bytes against the published hash and sequence. It verifies
observer shutdown preserves the source, native shutdown empties the owned job,
and the input desktop stays unchanged. A cleanup error fails the test.
It restores its process-local dashboard URL and does not change saved settings.

Additional existing entry points:

~~~powershell
python -B skills/stealth-ghost-delegation/scripts/smoke_test.py
python -B skills/stealth-ghost-delegation/scripts/desktop_watch_smoke_test.py --work-dir (Join-Path $Evidence 'pycharm-observer')
python -B skills/stealth-ghost-delegation/scripts/test_desktop_live.py --runtime-dir (Join-Path $Evidence 'window-lifecycle')
python -B skills/stealth-ghost-delegation/scripts/rea_smoke_test.py --browser $BrowserExe --rea-cli $ReaCli --work-dir (Join-Path $Evidence 'browser') --output (Join-Path $Evidence 'browser-proof')
~~~

Set BrowserExe and ReaCli to verified installed executable/script paths before
running the last command. The browser test uses a separate profile and creates
multiple owned sessions during its lifecycle and cleanup checks. It does not
reuse personal sign-in, cookies, or tabs. The PyCharm test uses a copied neutral
project and its own IDE configuration. Review captured PNGs as well as hashes.

## Behavior matrix

| Surface | Required evidence | Limits |
| --- | --- | --- |
| MCP discovery | Every supported tool appears with required root arguments, boolean annotations, matching version and documentation | Static schemas alone do not prove a host's normalized schema |
| Readiness | Native, browser, IDE and dependency results agree with actual prerequisites; missing IDE does not erase missing dependencies | One configured PC is not clean-machine proof |
| Native lifecycle | Owned inactive desktop, matching window/process identity, status, graceful stop and empty owned job | Starting an EXE does not supply arbitrary native controls |
| Browser | Separate profile, owned endpoint, scenario fill/click/wait, expected DOM, image hash, rejection of caller endpoint override, cleanup | No access to existing personal browser sessions |
| PyCharm | Correct copied root, run selection, console input/exit, GUI value/invoke, capture hashes, stale/unowned targets rejected, source unchanged | Python project adapter; not a universal language IDE adapter |
| Observer/dashboard | Fresh owned windows, truthful capture backend, source unaffected by observation or observer stop, input desktop unchanged | Window reconstruction; no universal video/audio recording or guaranteed FPS |
| Cached MCP image | Actual image content block, published exact hash/sequence, freshness, identity and mismatched-reference rejection | Reads an existing published frame; never triggers target capture |
| Generic command | Exact stdout/stderr/return code, timeout/error behavior | Account-level command execution; GUI isolation is not guaranteed |
| Installer/package | Source/manifest/archive parity, supported prerequisites, separate dependency cache identity, isolated install/upgrade/rollback, config preservation | Fixture tests do not replace a fresh Windows installation |
| Icon/publisher | Exact approved ninja image in source and installed manifests; Nexusmap.ai attribution | Cloud connector and local plugin are different distributions |
| Remote authorization | Login/consent/explicit paired PC, second-account rejection, expired/revoked credentials, replay/alias/rate limits, logout/unlink/tunnel cleanup | Unit tests and historical results do not replace fresh exact-release acceptance |
| Public submission | Verified publisher identity, public support/privacy/terms, recorded demo and executable reviewer examples | Do not fabricate legal policies, reviewer results, or identity verification |

## Required release evidence

Keep the exact PC package, portable plugin, gateway and authority commit/hash
compatibility record. Verify actual tool discovery and one harmless call in each
supported host after installation/reload. Confirm the installed icon and metadata.
Check a clean Windows installation and an upgrade preserving existing account and
device settings, then test rollback. Record unavailable environments as blocked.

A passing test suite covers its stated scenarios only. External runtimes may
execute through run_command without having a dedicated project/build/debug
adapter. Do not describe that as verified support for every programming language.
