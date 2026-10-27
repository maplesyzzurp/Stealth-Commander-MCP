# REA browser adapter and local viewer

This optional adapter launches a fresh Chrome process (Edge is also supported) on a separate Windows desktop. REA connects to that owned browser to run a JSON scenario. The original PyCharm coordinator, Java bridge, and smoke test are unchanged.

This adds browser workflows. It does not add arbitrary Windows-app or Electron control. A separate desktop isolates GUI input; processes still have the Windows account's filesystem and network permissions.

The browser inside the hidden desktop has its own profile. It does not inherit
the user's signed-in accounts, extensions, bookmarks, or history. Opening the
dashboard in personal Chrome does not connect that profile to the hidden
browser. This adapter does not support attaching an already-running personal
Chrome profile; disclose that distinction before using it for an authenticated
browser task.

## Start and run

Install REA using [REA-SETUP.md](REA-SETUP.md). Windows, the existing Python dependencies, Node.js, and a local Chrome-family browser are required. Run these commands from this skill's directory; change the browser and installation paths to match the computer:

```powershell
$scripts = '.\scripts'
$rea = 'D:\Tools\stealth-hidden-desktop\rea\node_modules\rea-agents\scripts\rea.mjs'
$started = python "$scripts\rea_bridge.py" start `
  --browser 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  --rea-cli $rea --work-dir 'D:\DesktopAutomation\sessions' | ConvertFrom-Json
if (-not $started.ok) { throw $started.error }
$session = $started.result.session
python "$scripts\rea_bridge.py" scenario --session $session `
  --file '.\examples\browser_scenario.json'
```

The included example fills a local order form, clicks Calculate total, and checks the visible result through REA's captured DOM and screenshots. The expected result is `3 × Notebook: $37.50`.

For another web app, supply a JSON scenario with its HTTP(S) `start_url`, REA actions, and requested captures. Omit `browser`: the adapter rejects caller-selected endpoints and launch modes and inserts its own verified connection and page. `{"url":"demo"}` selects the bundled local demo. REA navigates the selected page at the start of each scenario. Send one scenario at a time; a timeout means the outcome needs inspection before retrying.

## Watch it

In a separate terminal:

```powershell
python "$scripts\observe.py" --session $session
```

Open the returned local URL in a browser. The page refreshes a native screenshot about once per second and shows capture time, hash, process status, and stale/error indicators. It has no mouse, keyboard, or control endpoints. It listens only on `127.0.0.1`, uses a random URL token, and checks the Host header. This is a local viewer, not a remotely published service or a same-user security boundary.

These are periodic still captures of the owned browser window, not a full-desktop video stream. The adapter captures with `PrintWindow`; REA also returns page-level screenshots and DOM/accessibility evidence. Those two capture types are labeled separately.

The viewer cannot switch desktops or take input. It retains the last image after the session stops and labels it stopped. Close the viewer terminal with Ctrl+C when finished. The browser session is stopped separately.

## Inspect and stop

```powershell
python "$scripts\rea_bridge.py" status --session $session
python "$scripts\rea_bridge.py" stop --session $session
```

Check `ok`, `status`, and `cleanup` in the returned JSON. Session files remain for inspection:

| File | Purpose |
|---|---|
| `browser-session.json` | Browser identity, desktop, current status, heartbeat, capture error, cleanup result |
| `events.jsonl` | Startup, scenario, capture recovery/error, and shutdown events |
| `captures/latest.json` | Latest native PNG path, SHA-256, capture time, ownership and desktop metadata |
| `captures/*.png` | Up to 60 recent unedited native window captures |
| `requests/` | Scenario requests, REA responses, requested page evidence and stderr |
| `worker.log`, `keeper.log` | Local diagnostics |

Runtime files can contain URLs, page contents, paths, or credentials from the selected task. They stay outside the distributable package. The browser profile, request evidence, and logs remain on disk after stop; review them before sharing or deleting them. Native captures use a rolling 60-frame limit, but request evidence is retained.

REA marks event completeness as incomplete for attached browsers because it cannot recover pre-attachment events. Requesting console, page-error, and network events captures supported observations during the scenario; it does not establish a full process history. Inspect each requested artifact's state rather than treating the overall completeness flag as an action result.

The keeper owns a Windows Job Object. Worker and browser descendants enter it before running, and the keeper terminates the job on stop or worker exit. Each scenario verifies the exact browser process, profile, loopback listener, and selected page. A failed isolation check stops the session; there is no physical-input or ordinary-desktop fallback.

## Repeat the tests

```powershell
python "$scripts\rea_smoke_test.py" `
  --browser 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  --rea-cli $rea --work-dir 'D:\DesktopAutomation\tests' `
  --output 'D:\DesktopAutomation\test-evidence'
python -B -m unittest discover -s $scripts -p test_observe.py -v
```

The browser test checks the real REA action/result path, native captures, a changing browser viewer, rejection of unowned endpoints and caller browser overrides, normal shutdown, browser/worker exits, and a Job-assignment failure. Use the original [PyCharm test](commands.md) to verify the existing workflow on the target installation as well. A passing test establishes these workflows on that installation, not compatibility with every application or browser version.
