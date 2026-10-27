---
name: stealth-remote-control
description: Use Stealth Commander to inspect or operate a Windows PC the user owns or administers through its paired remote MCP connection, including supported hidden application sessions, commands, and observations.
---

# Stealth Commander remote control

Use the connected Stealth Commander MCP tools for the selected paired Windows PC. This package supplies remote workflow instructions, not a local runtime, shell, or installer.

## Establish the available workflow

- Use the tool names and argument schemas currently exposed by the connection. The PC's installed version determines its capabilities; a plugin version or this guide does not prove a tool is available.
- Use the authenticated selected PC. If the connection reports missing sign-in, consent, pairing, or an offline device, explain that state and use the host's connection flow. Do not substitute an unpaired address, account key, or direct tunnel.
- For readiness questions, call the available doctor tool and report each returned capability and missing dependency. Do not infer every workflow is ready from a top-level success flag.
- Obtain executable, interpreter, browser, dependency, and project paths from the connected PC's returned evidence or the user's verified inputs. Paths on the agent's own computer are not evidence about the remote PC.

## Run an owned session

1. Choose an exposed launch tool for the requested application. Native launch supplies an owned hidden session; it does not create a universal native click or typing adapter. Browser launch uses a separate browser profile. PyCharm project setup supports the project types described by the current tool and returns a copied project.
2. Supply every required argument from the live schema. Retain the exact returned session identifier or path and check the operation's result.
3. Inspect that session's status and ownership before further operations. Use only actions that the live schema supports for its kind. If the host schema omits a required session or action field, report the contract error instead of inventing arguments or bypassing the connection.
4. Inspect returned windows, controls, browser evidence, or IDE state before targeting an operation. Refresh selectors after UI changes. Request success is not proof that the application task passed; verify its actual output.
5. Stop the owned session when the task is finished, including after failure, unless the user asks to keep it active. Check shutdown evidence and report any uncertainty. Never stop unrelated sessions or processes.

Use a browser scenario operation only when the connected PC exposes it and its required session and scenario arguments are available. Use observer status, stop, or restart operations only when discovered. Do not infer these tools from the existence of similarly named local scripts.

## Observe and execute accurately

- Use capture_observation only if discovered. It exports an already-published validated cached image; it does not itself request a new capture or start an observer. Report stale, absent, or mismatched evidence as an error. Do not present an old frame as a new screenshot.
- Use supported observer or dashboard tools for their documented read or lifecycle behavior. Dashboard visibility does not imply arbitrary remote input, full-desktop video, or audio recording.
- When run_command is exposed, use explicit argv, a grounded working directory, and a bounded timeout. Commands operate with the selected Windows account's permissions. Do not treat a hidden console as proof that arbitrary GUI children remain on a hidden desktop; use a dedicated owned-desktop launch workflow for GUI work.
- Report actual stdout, stderr, exit status, and returned errors. After a timed-out mutation, inspect state before retrying.
- Preserve the user's physical mouse, keyboard, foreground desktop, personal browser profile, and existing IDE sessions. Do not use global input or desktop switching as a fallback.
- Hidden desktops separate GUI input; they do not restrict account filesystem or network privileges. Keep actions within the user's requested task.
- Remote tunnel setup and account administration are separate local setup workflows. Do not attempt unavailable tunnel tools or weaken authentication to complete an operation.

Finish with the verified result, any remaining active owned sessions, and unsupported or unverified behavior. Do not claim that changing this connector updated the PC runtime.
