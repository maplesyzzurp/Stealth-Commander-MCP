---
name: claude-archymanjini
description: "Use the original ARCHYMANJINI SSH reference to inspect an authorized remote Manjaro session from the paired Windows PC."
---

# Manjaro SSH route

The original entry is `USERPROFILE/.claude/skills/ARCHYMANJINI/SKILL.md`; its backing implementation is `USERPROFILE/.claude/skills/ARCHYMANJINI/scripts/manjini.ps1`. These files remain on the paired PC and were not copied or changed by this plugin.

## Host and source routing

This skill works with the existing authenticated Stealth Commander connection. In Codex on the source Windows PC, a local executor may read the original files directly. In ChatGPT desktop, web or mobile, local filesystem paths are on the selected paired PC, not on the chat device or a cloud shell.

Use the actual tool names and schemas exposed by the connection. If a PC-side file read or run_command tool is available, read the source through that tool using explicit argv and a bounded timeout. Resolve the source relative to that PC's USERPROFILE; do not substitute paths from a different machine. If the PC is offline, the source file is absent or the required tool is unavailable, report the missing capability without starting a tunnel, installer or alternate connection.

An availability, installation or skill-selection request does not authorize execution. For a later requested operation, preserve the user's windows, physical pointer, keyboard focus, personal Chrome session and unrelated processes. A second monitor, hidden console or SSH connection alone does not prove independent GUI input. Use only verified owned targets and supported operations. Report actual return status; a registered skill does not prove its backing workflow ran successfully.

## Requested operation

Use only a currently verified SSH target and preserved host-key trust. Historical host addresses and paths are hints, not target identity. The ydotool route affects a physical remote KDE session and does not establish separate input. Limit work to requested inspection or capture unless an independently owned isolated remote session has been established. Never run _watch_diag.ps1 trust replacement, loginctl unlock-session, or shared physical-session input as a fallback.

