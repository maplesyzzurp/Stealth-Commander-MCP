---
name: claude-multi-monitor-check
description: "Inspect the paired Windows PC's displays using the original multi-monitor-check guidance while preserving window layout and input focus."
---

# Monitor observation

The original entry is `USERPROFILE/.claude/skills/multi-monitor-check/SKILL.md`. These files remain on the paired PC and were not copied or changed by this plugin.

## Host and source routing

This skill works with the existing authenticated Stealth Commander connection. In Codex on the source Windows PC, a local executor may read the original files directly. In ChatGPT desktop, web or mobile, local filesystem paths are on the selected paired PC, not on the chat device or a cloud shell.

Use the actual tool names and schemas exposed by the connection. If a PC-side file read or run_command tool is available, read the source through that tool using explicit argv and a bounded timeout. Resolve the source relative to that PC's USERPROFILE; do not substitute paths from a different machine. If the PC is offline, the source file is absent or the required tool is unavailable, report the missing capability without starting a tunnel, installer or alternate connection.

An availability, installation or skill-selection request does not authorize execution. For a later requested operation, preserve the user's windows, physical pointer, keyboard focus, personal Chrome session and unrelated processes. A second monitor, hidden console or SSH connection alone does not prove independent GUI input. Use only verified owned targets and supported operations. Report actual return status; a registered skill does not prove its backing workflow ran successfully.

## Requested operation

Obtain the actual current monitor identities from available PC evidence; historical ASUS labels are not authoritative. Observe only what the supported capture interface can return. Do not raise, move, minimize or relaunch an app to expose it. If current tools cannot capture another display, report that limit instead of substituting shared-desktop input.

