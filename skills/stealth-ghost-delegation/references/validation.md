# Validation

Run checks from the skill directory with the Python interpreter used for the harness. Keep generated evidence outside the source checkout. Inspect JSON results and the actual captured images; an exit code or successful launch alone is not proof of a completed task.

## Readiness

```powershell
.\scripts\harness.ps1 doctor
```

For PyCharm, require `ok` and `result.ready`, then resolve any missing package or IDE findings. The GUI examples require PyQt6 in the selected project interpreter. The dashboard UI test requires Node.js. Browser integration also needs the pinned REA installation described in [REA setup](REA-SETUP.md).

## Automated checks

```powershell
python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

The suite covers observer responses, capture routing, window discovery, desktop registry ownership, UI behavior, resource metrics, and the packaged launch and maintenance helpers. Tests use neutral temporary data. Unit-test discovery does not execute the separate live-test entry points below.

## Live PyCharm check

Follow [Live self-test](commands.md#live-self-test) to run the neutral console and GUI examples, verify source preservation and input isolation, inspect captures, and check shutdown.

To exercise PyCharm while the observer runs:

```powershell
python scripts/desktop_watch_smoke_test.py --work-dir 'D:\DesktopObserverTest'
```

Choose a writable directory with enough free space. This creates a copied test project and an isolated IDE, verifies console and GUI work during observation, and stops its own session. Use `--help` for an explicit PyCharm installation override.

## Live window discovery check

```powershell
python scripts/test_desktop_live.py --runtime-dir 'D:\DesktopObserverTest\window-lifecycle'
```

This separate test creates neutral Qt fixtures on a new hidden desktop. It checks independent application windows, movement, stacking, popups, changed captures, input isolation, and stopping the observer without closing the source application. It cleans up its own fixture processes afterward. Inspect its final JSON and evidence for failures.

## Browser and native applications

The [browser guide](REA-INTEGRATION.md#repeat-the-tests) documents the real REA scenario, capture, ownership-rejection, and cleanup checks. The [native desktop guide](native-desktops.md) provides an explicit Notepad launch and stop sequence.

Browser playback, authenticated sessions, GPU-only content, and applications without a control adapter require their own task-specific verification. Passing neutral examples does not establish these capabilities.

## Before distributing an update

Check relative documentation links and install the complete skill folder in a fresh path. Confirm CLI help and readiness work from that copy. Run tests relevant to changed behavior, and use the live checks when a change affects application startup, capture, or shutdown. Review the exact Git diff and untracked files for personal paths, credentials, runtime state, and captures before committing.

The repository should contain the reusable implementation and its checks. Keep machine-specific run reports and exploratory notes outside it.
