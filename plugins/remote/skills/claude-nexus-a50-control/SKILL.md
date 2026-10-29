---
name: claude-nexus-a50-control
description: "Use the original nexus-a50-control reference for an explicitly requested dashboard demo on the user's identified Android phone."
---

# A50 dashboard route

The original entry is `USERPROFILE/.claude/skills/nexus-a50-control/SKILL.md`; its backing implementation is `USERPROFILE/.claude/skills/nexus-a50-control/scripts/bringup.ps1`. These files remain on the paired PC and were not copied or changed by this plugin.

## Host and source routing

This skill works with the existing authenticated Stealth Commander connection. In Codex on the source Windows PC, a local executor may read the original files directly. In ChatGPT desktop, web or mobile, local filesystem paths are on the selected paired PC, not on the chat device or a cloud shell.

Use the actual tool names and schemas exposed by the connection. If a PC-side file read or run_command tool is available, read the source through that tool using explicit argv and a bounded timeout. Resolve the source relative to that PC's USERPROFILE; do not substitute paths from a different machine. If the PC is offline, the source file is absent or the required tool is unavailable, report the missing capability without starting a tunnel, installer or alternate connection.

An availability, installation or skill-selection request does not authorize execution. For a later requested operation, preserve the user's windows, physical pointer, keyboard focus, personal Chrome session and unrelated processes. A second monitor, hidden console or SSH connection alone does not prove independent GUI input. Use only verified owned targets and supported operations. Report actual return status; a registered skill does not prove its backing workflow ran successfully.

## Requested operation

Verify the exact owned phone serial and the requested dashboard's current readiness before a phone demo. The original bring-up helper lacks serial selection and reliable failure checks; do not execute it when its target or prerequisites are uncertain. Use inspected serial-bound primitives for authorized phone actions and verify each result. Reading this skill does not authorize nexus-refresh, stopping the desktop app, a server launch, a public listener or a tunnel.

