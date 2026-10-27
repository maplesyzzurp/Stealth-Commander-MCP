# Native application desktops

`native_desktop.py` creates a new inactive Win32 desktop and starts an explicitly selected application there. Each start creates a separate owned session. This supplies process and desktop lifecycle, not an application-specific control adapter.

It does not move existing windows, reuse a personal application process, or switch the input desktop. A guardian holds the desktop and a Windows Job Object. The application enters the job before its suspended launch is resumed. Startup requires a visible job-owned window on the new desktop; an app that exits or redirects to an existing instance is a failed launch.

Use the REA adapter for browsers and the PyCharm harness for IDE workflows. This native launcher rejects common browser executables; it does not configure an isolated browser profile.

## Open Notepad

Run from this skill directory. Choose a new session path and a writable work directory outside the repository. This example finds the installed Windows Notepad package without borrowing a running process ID:

```powershell
$scripts = Join-Path (Get-Location).Path 'scripts'
$work = Join-Path $env:TEMP ('hidden-notepad-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$document = Join-Path $work 'Example.txt'
Set-Content -LiteralPath $document -Value 'Hello from a hidden desktop.' -Encoding utf8
$package = Get-AppxPackage -Name Microsoft.WindowsNotepad | Select-Object -First 1
if (-not $package) { throw 'Select an installed Notepad executable explicitly on this machine.' }
$notepad = Join-Path $package.InstallLocation 'Notepad/Notepad.exe'
$session = Join-Path $work 'session'
$started = python -B "$scripts/native_desktop.py" start --session $session `
  --executable $notepad --work-dir $work -- $document | ConvertFrom-Json
if (-not $started.ok) { throw $started.error }
```

For another native app, supply its existing absolute executable path, working directory, and arguments after `--`. Single-instance behavior and alternate-desktop compatibility depend on the app. The launcher cannot guarantee that an incompatible application will never attempt activity elsewhere; use a suitable adapter or isolated instance settings before trying it with personal applications.

Do not overwrite an existing session directory. Preserve the returned `result.session`, `result.desktop`, and `result.windows`. The state file is `native-session.json`; it is not a PyCharm `session.json` or a browser session.

## Observe it

The new desktop uses the harness prefix, so a dashboard with automatic discovery can find it. For an explicit observer:

```powershell
$window = $started.result.windows | Select-Object -First 1
$watch = python -B "$scripts/desktop_watch.py" start `
  --desktop $started.result.desktop --pid $window.pid --scope desktop `
  --interval 0.2 --port 0 --work-dir $work | ConvertFrom-Json
if (-not $watch.ok) { throw $watch.error }
```

Keep the returned watch directory. The observer validates the anchor and captures all visible top-level windows on that hidden desktop. See [Live view](LIVE-VIEW.md) for the combined dashboard.

## Inspect and stop

```powershell
python -B "$scripts/native_desktop.py" status --session $session
python -B "$scripts/native_desktop.py" stop --session $session
```

Stop posts close requests only to revalidated windows belonging to the owned job. If an app remains open, such as at an unsaved-work prompt, the response reports failure and the guardian stays alive. Inspect that state rather than claiming cleanup succeeded.

Only when discarding unsaved work is intended:

```powershell
python -B "$scripts/native_desktop.py" stop --session $session --force-owned-job
```

This terminates the session's job. A completed shutdown reports stopped state, no active owned processes, and desktop release. The job also terminates its processes if its guardian exits unexpectedly. Keep session data local and review it before deleting it. Stop an explicitly started observer separately; dashboard discovery manages observers it created itself.

## Several desktops

Repeat start with distinct session and work directories to create additional native desktops. Each is numbered separately by a discovery-enabled dashboard. A native session can contain the launched application's own child processes and windows; this CLI does not attach arbitrary existing applications or move them between desktops.

To reproduce a four-panel workspace, start a PyCharm session with [the harness](commands.md), two distinct Notepad sessions using this guide, and one owned Chrome session using [the browser adapter](REA-INTEGRATION.md). Attach an observer to each returned desktop and anchor, then pass the four returned watch directories to [the dashboard](LIVE-VIEW.md#combined-dashboard). Use neutral documents or the actual task inputs; no development-machine paths or saved demo sessions are needed.
