# Live window view

For multiple desktops on one page, use the combined dashboard described below.
It embeds one observer stream per desktop in a large tile. Expanding a tile
changes only the browser layout. The dashboard neither creates nor moves
applications between desktops.

The optional observer streams a reconstruction of visible windows on an existing
hidden harness desktop into a local browser. It uses each window's captured
position and stacking order over a dark background. It does not capture a native
desktop framebuffer, wallpaper, or every possible rendering surface.

The page opens with independent window panels. Each panel refreshes about once
per second, so overlapping applications remain visible together in the browser.
New windows appear automatically; closed windows disappear. Cards keep their
positions while captures update and label unavailable or stale images. Inspect
opens a larger view of one captured window. These controls only affect the page.

Select **Desktop layout stream** for the continuous MJPEG reconstruction in
native window positions and stacking order. Separate hidden desktops can each
have their own observer and browser tab, using different ports.

The viewer has no keyboard, mouse, clipboard, or window-control channel.
Observation starts a capture worker on the existing desktop. It does not switch,
move, resize, or close the source windows. Stopping the observer stops its worker
and server; it leaves the application running.

## Start

Run the command with the same Python interpreter used by the harness:

```powershell
python scripts/desktop_watch.py start --session <session-directory> --scope desktop --interval 0.2 --port 7070 --work-dir <observer-data-directory>
```

For an existing harness desktop without a session file, supply its exact desktop
name and a verified process on that desktop:

```powershell
python scripts/desktop_watch.py start --desktop <desktop-name> --pid <anchor-process-id> --scope desktop --interval 0.2 --port 7070 --work-dir <observer-data-directory>
```

Paths above are relative to this skill's directory.
The observer records the anchor process's executable and creation time and stops
if that identity no longer matches. The active input desktop is rejected.
Without `--scope desktop`, session mode watches the session process and its
descendants. Desktop mode discovers visible top-level windows across processes
on that specific hidden desktop.

Open the returned `url` in Chrome. Keep the entire URL, including its random
access token. The server binds only to `127.0.0.1`. It creates no firewall rule.
Port `0` selects an available port; a requested occupied port causes startup to
fail.

```powershell
python scripts/desktop_watch.py status --watch <returned-watch-directory>
python scripts/desktop_watch.py stop --watch <returned-watch-directory>
```

## Local HTTP contract

All paths below follow the token path in the returned URL. Requests require a
loopback connection and the exact `127.0.0.1:<port>` Host header.

| GET path | Response |
| --- | --- |
| token path itself | Dark window panels and a selectable workspace stream |
| `/stream.mjpg` | Continuous `multipart/x-mixed-replace` JPEG stream |
| `/workspace` | Window positions, stacking indices, capture states and hashes |
| `/meta` | Observer health and capture freshness |
| `/window?hwnd=…&sha256=…` | A validated current window PNG |
| `/frame?sha256=…` | The current representative window PNG |

JPEG parts include `Content-Length`, `X-Sequence`, `X-Captured-At`, and
`X-Frame-State`. Delivery is capped at 60 frames per second. Fresh capture rate
depends on window count, size, and application rendering time. Sequence numbers
count deliveries; capture timestamps identify source updates.

The stream retains its last captured content with a small stale indicator when
the source layout is more than three seconds old. An unavailable initial layout returns HTTP 503. Invalid tokens
return 404; unexpected Host headers return 403. POST, PUT, and DELETE return 405.
There is no remote input endpoint.

The dashboard has its own loopback address and random token. Its token path
serves the minimal page; `/meta` beneath that path returns the approved desktop
names, capture states, and observer URLs. It accepts only explicitly supplied
watch directories and verifies the source and server processes and listener
ownership. Invalid tokens return 404, unexpected Host headers return 403, and
write methods return 405. Local paths and raw error messages are omitted.

## Coverage

New visible windows are discovered on each capture pass. Closed windows disappear
from subsequent layouts. Minimized windows remain in the list but do not have a
rendered surface in the stream.

Capture is limited to 32 windows and 16 million pixels per window. The composed
view is scaled to at most 2560 pixels on its longest side and twelve million pixels
overall. Missing captures are represented visibly. Protected content, GPU
surfaces, transparent effects, brief popups, and application-specific rendering
may not reproduce correctly. A successful `PrintWindow` call does not prove that
every pixel was rendered.

The browser page is dark. Source screenshots retain their application colors.
Captures are sampled in sequence, so a reconstructed frame is not an atomic
snapshot of all windows at exactly the same instant. Capture can add CPU and
rendering work even though the viewer sends no input.

## Combined dashboard

`dashboard.py` combines approved observers on one token-protected loopback page.
Its `/meta` endpoint returns a UI build identifier and desktop entries with a
stable sequence number, status, approved stream URL, sanitized window list,
and resource samples. It exposes no input commands, source executable paths,
process command lines, or environment variables.

Resource values can be unavailable while counters warm up or when access fails.
They describe associated processes, not exclusive per-window consumption.
Browser renderers and other validated child processes count toward their parent
desktop. GPU aggregation groups counters by adapter and engine before choosing
the busiest measured engine; it does not add unrelated percentages.

The optional discovery registry starts observers only for eligible inactive
harness desktops. It reuses matching existing observers and stops only observers
it owns when a source disappears or the dashboard closes.

Run from this skill's directory, using watch directories returned by the observer:

```powershell
python scripts/dashboard.py `
  --watch '<first-watch-directory>' --name 'Code' `
  --watch '<second-watch-directory>' --name 'Notes' `
  --status-file 'D:\DesktopObservation\dashboard.json' --port 0 `
  --discover-root 'D:\DesktopObservation\discovered'
```

Open the URL written to the status file. `--scan-root` adds runtime directories
whose existing observers should be reused. The page supports Desktops and Windows
modes, fullscreen, panel resizing, drag ordering, and browser-local names. It
reloads after a server UI build change when the URL remains the same.


## Maintain a running viewer

Use these commands only during an intentional update of owned viewer services.
They replace the viewer processes while preserving their loopback URLs and source
applications. Supply the saved state and watch directories; never reconstruct them
from a browser label. Create a writable log directory outside the repository first.

```powershell
python -B scripts/restart_observer.py --watch '<watch-directory>' --runtime-dir '<existing-log-directory>'
python -B scripts/restart_dashboard.py --state '<dashboard-state-file>' `
  --runtime-dir '<existing-log-directory>' `
  --watch '<first-watch-directory>' --name 'Code' `
  --watch '<second-watch-directory>' --name 'Notes'
```

Repeat every intended watch and name. If discovery was enabled, include the
existing `--discover-root` and each `--scan-root` again. The helpers verify exact
process identities, launch paths, state paths, and loopback listener ownership
before stopping anything. Records with confirmed complete shutdown and exited workers are skipped.
Ambiguous or stale discovery entries require inspection. A failure is not
permission to kill a process by name.

When migrating from another script location, `--previous-launcher` must identify
that exact prior launcher. An observer anchor update requires both `--anchor-pid`
and its verified `--anchor-created` value, and only works in desktop scope.

Inspect the returned preservation checks and the restart log. A failed replacement
has no automatic rollback. Keep the previous code available until verification
succeeds. These commands preserve the address; they do not connect to or manipulate
personal Chrome. The loaded dashboard automatically reloads when its server reports
a changed UI build. If a separate targeted browser refresh is needed, use the
caller's authorized browser API without changing the active tab or physical input.
