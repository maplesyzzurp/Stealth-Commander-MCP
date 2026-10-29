# Stealth Commander

This repository and its configured `origin/main` are the source of truth for the complete build and future releases. The prior repository is historical; publish updates from this checkout to its configured origin.

Run supported Windows applications on hidden desktops and watch their captured windows in a local Chrome dashboard. The package contains the desktop launchers, PyCharm control bridge, optional REA browser adapter, capture workers, streaming server, dashboard UI, resource metrics, examples, and tests. No AI provider or model API key is required.

This repository is the authoritative source for the skill. An agent starts with [AGENTS.md](AGENTS.md), then [SKILL.md](skills/stealth-ghost-delegation/SKILL.md). Normal use leaves source and instructions unchanged. Improvements belong to an explicit maintenance task with verification and a versioned commit.

## PC installer

Current builds are distributed privately. Obtain the reviewed source archive or an approved npm `.tgz` from the operator. There is no public npm registry release or public download supplied by this repository. Remote account access is limited to approved accounts.

Use Windows x64, Node.js 18 or newer with npm, and 64-bit Python 3.11 or newer. The launcher finds a supported Python on PATH or uses `PYCHARM_HARNESS_PYTHON`. Installation downloads the hash-locked Python dependencies from PyPI. PyCharm and Chrome are separate prerequisites for their respective workflows; the optional REA adapter has the newer Node.js requirements listed below.

For an approved source archive, extract it and open PowerShell in the directory containing `package.json`:

~~~powershell
node ./bin/stealth-commander.js install
node ./bin/stealth-commander.js doctor
~~~

For an approved package archive, open PowerShell in the directory containing that file:

~~~powershell
npm install --global ./stealth-commander-1.1.3-private.5.tgz
stealth-commander install
stealth-commander doctor
~~~

Use the supplied archive's actual filename if it differs. The `install` command installs the complete skill and Python dependencies and updates supported existing local AI client configurations. It starts no services and needs no account. Read the individual readiness results from `doctor`; success does not mean every optional application is installed. Local CLI use from extracted source requires no global npm installation.

After a source build passes release checks, the operator can produce that npm archive with `npm pack --ignore-scripts` from the repository root. GitHub Actions validates a package dry-run; it does not distribute a downloadable build.

The remaining commands use the globally installed `stealth-commander` command. From extracted source, replace it with `node ./bin/stealth-commander.js` while staying in that source directory. `setup` requires existing pairing or an approved account key, updates the installation, and starts local services. It does not create a new remote tunnel.

To link an account and enable remote access, run `stealth-commander setup --enable-remote` in an interactive terminal. An unpaired PC prompts for the account key without echoing it. Remote requests require current account authorization, paired-PC selection, and a verified single-use request ticket. Commands use the Windows account's file and network permissions. Keep connection URLs and account keys private. The `tunnel` command and the portal's "Enable Temp Tunnel" control also explicitly enable remote access.

The local setup command does not stop an existing tunnel. Use "Lock Local-Only (Stop Tunnel)" in the portal to stop a tunnel. Setup does not change Chrome security policies.

PC pairing stores only the account email, this PC's device ID, and a device credential. The account key, login session, and device list stay out of the saved PC configuration. The device credential can update or revoke this PC's pairing; it cannot list or manage the account's other PCs. `whoami` verifies this pairing and prints no credentials. `logout` revokes this PC's device credential and clears its local pairing file.

Run `stealth-commander login` to enter the account key through the hidden prompt. Existing key and email/password flags remain supported for compatibility. The prompt keeps the key out of the command line and shell history. Noninteractive calls never prompt; an unpaired call returns a pairing-required result.

When an older pairing file contains account credentials or a cached account profile, the installer removes that file before login or remote startup. Pair the PC again. If the old file cannot be cleared, pairing and remote startup stop. No browser sign-in data is read or reused.

An existing local hub must report the installed server version before setup can reuse it. If its version is old or cannot be verified, setup returns `restart_required` before pairing or enabling remote access. Stop that hub and retry setup. The installer does not terminate existing processes.

## Start here

Use Windows x64, PowerShell, and 64-bit Python 3.11 or newer. PyCharm workflows also need an installed PyCharm distribution with Java and javac in its bundled runtime. The optional REA adapter needs Node.js ^22.19, ^24.11, or >=26 and its pinned package; observation does not require REA.

From the repository root:

```powershell
$skillRoot = Join-Path (Get-Location).Path 'skills/stealth-ghost-delegation'
$harness = Join-Path $skillRoot 'scripts/harness.ps1'
& $harness doctor
```

Read the JSON response. If harness packages are missing, run `& $harness setup`, then repeat `doctor`. PyCharm use requires both `ok` and `result.ready` to be true. Setup installs pywin32, Pillow, comtypes, and psutil in an ignored local dependency cache. PyCharm, Node.js, and application-specific dependencies are installed separately.

Choose the guide for the task. Commands in the guides use the skill directory as their base unless stated otherwise.

| Task | Guide |
| --- | --- |
| Run a Python project in PyCharm; inspect console and GUI results | [PyCharm commands](skills/stealth-ghost-delegation/references/commands.md) |
| Create a hidden desktop for a native app such as Notepad | [Native desktops](skills/stealth-ghost-delegation/references/native-desktops.md) |
| Watch one or several desktops; inspect CPU, RAM, GPU, and capture FPS | [Live view](skills/stealth-ghost-delegation/references/LIVE-VIEW.md) |
| Install the optional REA dependency | [REA setup](skills/stealth-ghost-delegation/references/REA-SETUP.md) |
| Run browser scenarios on an owned hidden Chrome instance | [Browser adapter](skills/stealth-ghost-delegation/references/REA-INTEGRATION.md) |
| Check an installation or a source change | [Validation](skills/stealth-ghost-delegation/references/validation.md) |

## Dashboard

One dark dashboard groups windows by hidden desktop. It includes Desktops and Windows views, numbered panels, inline rename, fullscreen, resizing, smooth drag ordering, and a refresh button for each panel. CPU, RAM, GPU, and capture FPS appear in each header. The UI and its assets are bundled with the Python server; there is no separate frontend build.

Automatic discovery can add eligible hidden harness desktops as they appear. The observer discovers visible top-level windows across processes. View controls affect the browser layout; they do not send mouse, keyboard, or clipboard input to the applications.

Capture uses Direct3D 11 shared-surface readback (`DwmGetDxSharedSurface`) with Windows `PrintWindow` fallback. The continuous MJPEG stream reconstructs captured windows in their positions and stacking order, with a target ceiling of 60 frames per second and adaptive idle cadence scaling. It is not a full-screen monitor mirror or remote-control interface. Protected DRM surfaces, minimized windows, and brief popups may be unavailable. Capture FPS and application GPU usage measure different things.

Local servers bind to loopback and use random access tokens. Remote access requires an explicit tunnel command or `setup --enable-remote`. Keep returned URLs private.

## Hand it to another agent

Give the agent this repository or its ZIP, the task, and the required input files. It needs local Windows command access; a chat-only model cannot execute the skill. The repository and bundled references supply the instructions without requiring the development conversation, and the JSON CLI is completely model-agnostic.

For a host that discovers installed skills, copy the complete `skills/stealth-ghost-delegation` folder into that host's skill directory while keeping its internal layout:

- **Antigravity (AGY):** `~/.gemini/config/skills/stealth-ghost-delegation`, `~/.gemini/skills/stealth-ghost-delegation`, or workspace `.agents/skills/stealth-ghost-delegation` (invoke with `/stealth-ghost-delegation` or natural language).
- **ChatGPT / Codex:** `~/.codex/skills/stealth-ghost-delegation` or `~/.agents/skills/stealth-ghost-delegation` (select `/stealth-ghost-delegation` or mention `$stealth-ghost-delegation`; `agents/openai.yaml` supplies host discovery metadata).
- **Factory Droid:** `~/.factory/skills/stealth-ghost-delegation` or `~/.agents/skills/stealth-ghost-delegation` (invoke `stealth-ghost-delegation` via the Skill tool or `/stealth-ghost-delegation`).

Direct CLI use requires no skill installation.

Run from an installed copy if the source checkout must remain unchanged, including generated caches. Keep project copies, browser profiles, session state, logs, and captures in a separate work directory. Never copy credentials or a running personal Chrome profile into the package.

The hidden browser adapter creates a separate profile. It does not inherit personal sign-in, extensions, bookmarks, or history. Viewing the dashboard in personal Chrome does not change that limitation. Use Chrome by default and disclose the separate profile before an authenticated task.

## What belongs here

- `AGENTS.md` and this README define repository use and entry points.
- `skills/stealth-ghost-delegation/SKILL.md` and `references/` contain the portable instructions.
- `scripts/` contains the implementation, dependency declaration, neutral tests, and Java bridge sources.
- `examples/` and the script examples provide inputs for documented checks.
- The repository's `.gitignore` (not part of the npm payload) keeps ordinary generated caches, runtime data, and credential files out of commits. It does not replace reviewing files before sharing.

Runtime files and development investigation notes are excluded. Tests and examples stay because they let the next maintainer verify behavior.

## Boundaries

A hidden desktop separates GUI input, but its processes retain the Windows account's file and network access. It is not a security sandbox. The harness uses targeted application control and must not fall back to the physical mouse, global keystrokes, or desktop switching.

Launching another application does not create a control adapter for it. PyCharm internals, accessibility support, browser behavior, and capture compatibility vary by version. Run the relevant checks on the destination machine.

Stop only sessions owned by the task. Preserve another session's apps and unsaved work. Inspect the returned shutdown result; a requested stop is not proof that every process exited.

## Components and coverage

The [capability map](skills/stealth-ghost-delegation/references/CAPABILITIES.md) distinguishes direct MCP tools, CLI helpers, and language support. The local plugin includes the PC runtime and operational skill. The account plugin connects to the HTTPS gateway; it does not install a PC runtime.

Gateway, authorization, billing-test and rate-limit source live under `services/`. Their example configuration files contain placeholders; deployment requires the existing private service bindings. Source pushes do not enable public access or production billing. See [release checks](docs/RELEASE.md).

## Refresh the local Codex plugin

Keep one reviewed source checkout. Installed skill copies and plugin caches are generated from that source; never edit a version cache as the marketplace source.

After verifying the release and manifest, run the installer-only command to update the canonical skill and existing client configurations without starting services:

~~~powershell
node bin/stealth-commander.js install
pwsh -NoProfile -File tools/install-codex-plugin.ps1 -WhatIf
pwsh -NoProfile -File tools/install-codex-plugin.ps1
~~~

The helper updates only the existing Stealth Commander marketplace entry, preserves a backup and unrelated plugins, runs Codex's supported install command, and verifies the complete installed package against the authored skill inventory, install manifest, and approved ninja image. It requires the checkout to be contained within the existing marketplace root. It refreshes an existing local plugin; it is not a first-time marketplace registration command.

Already-running MCP processes may retain their old tool declarations. Verify a fresh connection's server version and tools/list; a file update alone does not prove an active process reloaded. Existing application sessions and account pairing are not a reason to kill unrelated processes.

## Ongoing development

Use this repository as the source of truth. Ship small updates to the same plugin identity;
there is no need to recreate the plugin for each feature. Keep application-specific work
separate from the core hidden-desktop lifecycle.

For each release, raise the local version in package.json, both local plugin manifests,
and SERVER_INFO in the MCP script. The account connector under plugins/remote has its own
version and keeps the same existing plugin ID.

~~~powershell
python -B tools/update-manifest.py
npm run verify:package
npm test
git add .
git commit -m "Describe the desktop delegation update"
git push origin HEAD:main
~~~

GitHub Actions checks each push and pull request. Refresh local installations using the
commands above; update the existing account plugin only when its own files change.
A source push does not automatically deploy hosted services or publish a public listing.
