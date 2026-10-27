# Capabilities and tool contract

Use this map with the actual server's tools/list result. Local setup, pairing and tunnel management remain separate from remote task execution. A remote gateway deliberately withholds start_tunnel.

| MCP tool | Purpose and required inputs |
| --- | --- |
| `doctor` | Report platform, dependency and workflow readiness. Inspect each readiness result. |
| `dashboard_status` | Read dashboard and capture-health information; may initialize the local dashboard. |
| `list_hidden_desktops` | Enumerate eligible owned desktops; may initialize dashboard/probe state. |
| `start_native_app` | Launch executable with work_dir and a new session directory on an owned inactive desktop. |
| `start_browser` | Launch a separate Chrome-family profile with explicit browser, rea_cli and work_dir paths. |
| `start_pycharm` | Copy the selected Python project and create an isolated IDE session. |
| `session_action` | Supply kind, action and the exact returned session. Follow the action table below. |
| `start_observer` | Supply work_dir and either session or both desktop and pid. Observe only a verified inactive target. |
| `observer_action` | Read or stop an owned watcher with action=status or stop and its exact watch path. |
| `capture_observation` | Return an already-published validated cached PNG. It does not start observation or request a fresh frame. |
| `run_command` | Execute argv with optional cwd/timeout and return stdout, stderr and returncode. Account-level background command, not a GUI isolation guarantee. |
| `start_tunnel` | Explicit local remote-access setup. Intentionally absent from remote discovery/calls. |

## Session actions

| Kind | Actions | Action-specific fields |
| --- | --- | --- |
| `native` | status, stop | force_owned_job is an explicit owned-session cleanup option when applicable. |
| `browser` | status, scenario, stop | scenario requires scenario_file. Its JSON must omit caller-selected browser/CDP targets; the bridge inserts the verified owned page. |
| `pycharm` | status, windows, controls, invoke, value, click, ide, capture, stop | Resolve hwnd from current windows. controls/invoke/value/click/capture use the grounded window. invoke/value use current index and expected label; value needs text. click requires x/y. capture uses output. |
| pycharm / ide | dump, click, toggle, text, trust-project, dismiss-tour, roots, open, select, run, input, content, layout | Set ide_action and its typed name/path/text/selected fields as applicable. Inspect current IDE state before choosing targets. |

Required fields remain at the root of the JSON schema so clients do not lose session during union normalization. Invalid kind/action combinations must fail before a process starts. Legacy extra_args cannot replace the owned session, command or target fields.

## CLI helpers and boundaries

Use scripts/rea_bridge.py for the browser start/status/scenario/stop CLI. Use desktop_watch.py for watcher start/status/stop and restart_observer.py for a restart preserving its URL. restart_dashboard.py preserves dashboard URL/source identities. These helper files are included, even where a direct MCP operation covers only their common action.

Use local installer/account commands for install, setup, login, register, whoami, logout, serve and tunnel. Do not turn account management or pairing into an unauthenticated remote operation.

Launching a native executable provides launch/status/stop and observation. It does not create application-specific input controls. PyCharm controls use the owned Java bridge; browser actions use the owned REA connection. Never substitute global input or an existing personal application.

## Programming languages

Python has a project-copy/PyCharm configuration adapter. The Java sources implement IDE attachment and controls; they are not a Java-project adapter. JavaScript implements the launcher and browser/gateway components. PowerShell provides setup/wrappers.

run_command can invoke an installed compiler or interpreter with explicit argv. Detect and validate that runtime for the requested task. This does not imply an integrated build/debug adapter for C#, TypeScript, C/C++, Java, Kotlin, Go, Rust or other languages.

REA is an optional third-party dependency. Stealth integrates its capture-browser-scenario operation. Other upstream REA language/platform tools are not connected to Stealth automatically.

## Observation

Windows are captured using the available D3D11 or PrintWindow backend; inspect actual per-frame provenance. Viewers reconstruct captured windows, not a full desktop framebuffer. No audio/video-recording or universal application-control feature is implied. A 60 FPS setting is a target ceiling; use observed FPS and health for the actual result.

Inspect image freshness, ownership, process identity and hash. Cached image export can fail correctly when no suitable fresh published frame exists. Keep raw captures and machine-specific logs outside the source repository.
