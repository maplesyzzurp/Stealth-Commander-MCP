---
name: claude-stealthdroid
description: "Use the original stealthDroid ADB workflow for screenshots or authorized control of the user's selected Android device through the paired Windows PC."
---

# Stealth Android

The original entry is `USERPROFILE/.claude/skills/stealthDroid/SKILL.md`; its backing implementation is `USERPROFILE/.claude/skills/stealthDroid/scripts/droid.ps1`. These files remain on the paired PC and were not copied or changed by this plugin.

## Host and source routing

This skill works with the existing authenticated Stealth Commander connection. In Codex on the source Windows PC, a local executor may read the original files directly. In ChatGPT desktop, web or mobile, local filesystem paths are on the selected paired PC, not on the chat device or a cloud shell.

Use the actual tool names and schemas exposed by the connection. If a PC-side file read or run_command tool is available, read the source through that tool using explicit argv and a bounded timeout. Resolve the source relative to that PC's USERPROFILE; do not substitute paths from a different machine. If the PC is offline, the source file is absent or the required tool is unavailable, report the missing capability without starting a tunnel, installer or alternate connection.

An availability, installation or skill-selection request does not authorize execution. For a later requested operation, preserve the user's windows, physical pointer, keyboard focus, personal Chrome session and unrelated processes. A second monitor, hidden console or SSH connection alone does not prove independent GUI input. Use only verified owned targets and supported operations. Report actual return status; a registered skill does not prove its backing workflow ran successfully.

## Requested operation

For a requested phone action, establish the exact authorized device serial on the selected PC first. The small original helper has no serial option, uses a shared capture staging name, and can print success after a failed ADB command. Do not use an ambiguous first-device fallback. Prefer explicit serial-bound ADB arguments or an existing inspected serial-aware adapter, and check exit status and fresh output. Never bypass a secure lock. Phone operations must not use the PC's shared pointer or keyboard.

