# Commands

Set `$skillRoot` to the directory containing this skill's `SKILL.md`. Replace the example project, script, and interpreter with the task's actual paths.

```powershell
$harness = Join-Path $skillRoot 'scripts/harness.ps1'
& $harness doctor
```

Every normal command returns JSON with `ok` and either `result` or `error`. Check `ok` before reading `result`. `doctor` also reports `result.ready`.

If the reported harness packages are missing:

```powershell
& $harness setup
& $harness doctor
```

Core packages are pywin32, Pillow, comtypes, and psutil. `setup` installs them into the harness's local dependency directory. It does not install project libraries or PyCharm. Set `PYCHARM_HARNESS_PYTHON` for the current process to choose the CLI interpreter. Pass `--pycharm` or set `PYCHARM_HARNESS_IDE` if IDE discovery fails.

## Start and inspect

```powershell
$started = & $harness start --project 'C:\Projects\Example' --run 'Main=main.py' | ConvertFrom-Json
if (-not $started.ok) { throw $started.error }
$session = $started.result.session
& $harness status --session $session
& $harness windows --session $session
& $harness ide --session $session --action dump
```

Read `project_ready` and `attention` from the start result. Keep the session path even if readiness is false so the session can be inspected and stopped. A session copies the chosen project, excluding common caches, virtual environments, repository metadata, and symlinks or junctions. Select a project interpreter with `start --python '<python.exe>'`; do not assume an excluded virtual environment was copied.

Repeat `--run 'Name=relative_script.py'` for multiple entry points. Use `--env KEY=value` or `--env-file '<json-file>'` when the project requires environment values. An environment file is a JSON string map. Keep credentials out of command transcripts and returned evidence. `--work-dir` chooses where the per-session workspace is created; otherwise the harness uses its default local session directory.

## Run and read the console

```powershell
& $harness ide --session $session --action open --path 'main.py'
& $harness ide --session $session --action select --name 'Main'
& $harness ide --session $session --action run
& $harness ide --session $session --action content --name 'Main'
```

Execution is asynchronous. Poll the console at bounded intervals until the expected result or a failure appears. For an input prompt:

```powershell
& $harness ide --session $session --action input --text 'sample'
& $harness ide --session $session --action content --name 'Main'
```

IDE actions also include `click`, `text`, `toggle`, `roots`, `layout`, `trust-project`, and `dismiss-tour`. Use a fresh `dump` to identify controls. `click` and `text` accept a current index or an exact unique label; `toggle` requires the expected label and `--selected true` or `false`. `layout` arranges the editor and console in the private IDE. Unknown dialogs require inspection before continuing.

## Use a GUI window

Choose `$windowHandle` from the current `windows` result, then inspect controls:

```powershell
& $harness controls --session $session --hwnd $windowHandle
```

For the bundled example, use the current indices for its `Value` field and `Apply` button:

```powershell
& $harness value --session $session --hwnd $windowHandle --index $fieldIndex --label 'Value' --text 'sample'
& $harness controls --session $session --hwnd $windowHandle
& $harness invoke --session $session --hwnd $windowHandle --index $buttonIndex --label 'Apply'
& $harness controls --session $session --hwnd $windowHandle
```

Re-read indices after each UI change. Prefer accessible value and invoke patterns when supported. `click` provides window-relative coordinates for owned windows; read `click --help` and inspect a recent capture before using it. It posts messages to the selected window without using the physical cursor.

## Capture and stop

```powershell
& $harness capture --session $session --hwnd $windowHandle --output 'C:\Captures\example.png'
& $harness stop --session $session
```

Capture uses Windows `PrintWindow`. The PNG and its `.capture.json` sidecar record dimensions and a SHA-256 hash. Existing output images or sidecars are refused. View the PNG to establish that it rendered the intended state; a hash only verifies file identity.

For an IDE screenshot that must show source code, open the intended file again immediately before capturing it. PyCharm can open a What's New tab after startup while the console run still succeeds. Check the selected editor, project tree, console, and visible paths before using the image in a report. The self-test verifies console output and capture integrity; it does not guarantee that the code editor is the selected tab.

Stop requests closure and then terminates remaining owned processes after validating process identity. Read its `stopped` result. Sessions and evidence remain on disk for inspection. Do not delete them as an automatic substitute for shutdown.

## Live self-test

Use the same Python interpreter for startup and the test. PyQt6 must be installed in it for the neutral GUI example. The test launches a real isolated IDE, changes only the copied examples, captures two windows, checks rejected invalid targets, and stops its processes. It does not use coursework or other user projects.

```powershell
$python = 'C:\Path\To\python.exe'
$scripts = Join-Path $skillRoot 'scripts'
& $python (Join-Path $scripts 'smoke_test.py')
```

For explicit control of workspace placement, start the test session first:

```powershell
$testStart = & $python (Join-Path $scripts 'harness.py') start --project (Join-Path $scripts 'examples/smoke_project') --run 'Console example=console_demo.py' --run 'GUI example=gui_demo.py' --work-dir 'C:\Work\DesktopTest' | ConvertFrom-Json
if (-not $testStart.ok) { throw $testStart.error }
& $python (Join-Path $scripts 'smoke_test.py') --session $testStart.result.session
```

If startup or the test fails before its cleanup runs, inspect the saved session and use `stop` explicitly. The test accepts an existing session only for these bundled examples and stops it at the end.

Review `smoke-test.json` in the returned session directory. Check all 13 named results and `shutdown.stopped`, then view both captures. Test coverage includes input-desktop isolation, the copied project root, console input and exit, GUI interaction, capture hashes, ownership rejection, path confinement, metadata preservation, IDE profile identity, process creation time, active-desktop rejection, source preservation, and shutdown.
