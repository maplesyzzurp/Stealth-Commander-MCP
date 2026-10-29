---
name: claude-stealth-use
description: "Use the original stealth-use guidance for authorized work on the paired Windows PC while preserving the user's shared workspace and input."
---

# Stealth workspace guidance

The original entry is `USERPROFILE/.claude/skills/stealth-use/SKILL.md`. These files remain on the paired PC and were not copied or changed by this plugin.

## Host and source routing

This skill works with the existing authenticated Stealth Commander connection. In Codex on the source Windows PC, a local executor may read the original files directly. In ChatGPT desktop, web or mobile, local filesystem paths are on the selected paired PC, not on the chat device or a cloud shell.

Use the actual tool names and schemas exposed by the connection. If a PC-side file read or run_command tool is available, read the source through that tool using explicit argv and a bounded timeout. Resolve the source relative to that PC's USERPROFILE; do not substitute paths from a different machine. If the PC is offline, the source file is absent or the required tool is unavailable, report the missing capability without starting a tunnel, installer or alternate connection.

An availability, installation or skill-selection request does not authorize execution. For a later requested operation, preserve the user's windows, physical pointer, keyboard focus, personal Chrome session and unrelated processes. A second monitor, hidden console or SSH connection alone does not prove independent GUI input. Use only verified owned targets and supported operations. Report actual return status; a registered skill does not prove its backing workflow ran successfully.

## Requested operation

Apply the original workspace-preservation guidance to the user's requested work. Adapt historical screenshot, monitor and browser tool names to tools actually exposed by the selected PC. File and CLI work, and verified owned isolated GUI operations, are available when requested and supported. Do not request access to every application, surface shared-desktop windows, or treat the old normal-mode escape as permission to change the user's isolation rule.

