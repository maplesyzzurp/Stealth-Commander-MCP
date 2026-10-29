# Local automation instructions

This repository is the authoritative source for Stealth-Ghost-Delegation. Read [the skill](skills/stealth-ghost-delegation/SKILL.md) before operating a project and follow its bundled references. Use this checkout's version consistently; do not mix scripts from other copies or reconstruct instructions from an earlier conversation.

Ordinary use must not edit the skill's source, examples, instructions, or dependencies list. Make source changes only during an explicitly requested maintenance task, verify them, and commit them as a reviewed update. If execution reveals a bug, report the failing command and evidence rather than silently rewriting the skill.

Use the JSON CLI in `skills/stealth-ghost-delegation/scripts`. Check `ok` in every response and keep the exact session path returned by `start`. For PyCharm, run `doctor` before installation or startup. For REA browser work, follow [the browser reference](skills/stealth-ghost-delegation/references/REA-INTEGRATION.md) and use its separate browser session. A readiness-only request must not launch a project.

Use the project and entry points supplied by the user or established in the task. If no project is identified, check readiness and ask for the project path. Do not infer permission to run an unrelated recent project.

Operate only on the harness's separate desktop and owned process tree. Never fall back to physical mouse input, shared keyboard input, desktop switching, or a personal IDE session. Inspect current windows and controls before choosing targets. Treat a timeout as an unknown result and inspect state before retrying a mutation.

A project runs in a copy. Report and review changes before applying them to the source within the user's authorized scope. Keep runtime data, credentials, and captures outside submitted or published code unless they are explicitly needed and checked.

Use `stop` when finished and inspect its result. Preserve another session's processes and files. If the user asks to keep a session active, report that fact and retain its path.

When changing this repository, keep the command interface model-agnostic and usable across Antigravity, ChatGPT, Codex, Factory Droid, and any local Windows CLI caller without a particular model or AI service. Test behavior through the included examples. Do not add personal paths, account names, credentials, coursework, saved sessions, or raw captures to the repository.

Keep project copies, runtime state, logs, and captures in a work directory outside the checkout. Setup may create ignored dependency caches; use an installed copy of the complete skill directory if the checkout must remain byte-for-byte unchanged. The ignore file prevents ordinary accidental commits of generated files; it is not a secret scanner.

Prefer Chrome. The hidden browser adapter uses a separate profile; explain that limitation before an authenticated task and never present it as the user's signed-in browser. Do not alter existing personal tabs, browser settings, or input focus.

## Canonical repository

This repository and its configured `origin/main` are the source of truth for the complete build. Use this checkout and its configured origin for future changes and pushes. The prior repository is historical; do not publish new builds there. Preserve the existing Stealth Commander account-plugin identity when releasing updates.
