# DevHard

**English** | [한국어](README.ko.md)

Workspace launcher for independent Chrome profiles and SSH SOCKS5 tunnels.

Choose **Dark → DevHard** in the UI for the artwork-inspired charcoal, silver and
red color scheme, or **Light → DevHard Light** for its silver-white counterpart.
Both work with Rounded, Flat and Terminal styles.

M4 implements the data store, workspace registry, Manager lock, port reservations,
independent SSH/Chrome controls, workspace Start All/Stop All and a local management UI.
Product requirements and development records live in the sibling `../notes/`
directory.

## Development

Python is pinned to **3.13.16** by `mise.toml`. The HTTP adapter uses FastAPI and
Uvicorn; core process/storage code uses the standard library. `uv.lock` records
resolved dependencies, and mise activates the project `.venv`.
Tunnel controls require OpenSSH, `ps`, and `lsof`; macOS process identity
uses system libproc and normal application quit uses AppKit through ctypes. Linux execution is scheduled for M5.

```sh
mise trust
mise install
mise run setup
mise run test
mise run devhard -- --help
```

Use the same `--data-dir` before the subcommand for every invocation; omit it to
use `~/.devhard`. Workspace names use lowercase letters, digits, `_` and `-`.

```sh
mise run devhard -- --data-dir .devhard workspace add dev-a --ssh-host dev-a --tab http://127.0.0.1:3000
mise run devhard -- --data-dir .devhard workspace list
mise run devhard -- --data-dir .devhard workspace show dev-a
mise run devhard -- --data-dir .devhard manager run --open
# In another terminal:
mise run devhard -- --data-dir .devhard manager status
```

`manager run` serves the UI on `127.0.0.1` at an automatically selected HTTP port,
prints the `api_url` in its JSON ready snapshot, and stays in the foreground until
Ctrl-C or SIGTERM. `--open` opens that address in the normal default browser.
Use `--port 61400` for a fixed management port. The HTTP port is separate from
workspace SOCKS reservations. A second Manager for the same physical data directory exits
with code 73. `workspace add` takes the same lock and currently requires the
foreground Manager to be stopped; read-only list/show/status work while it runs.
Use the UI/API to mutate while the foreground Manager is running.

## Management UI and API

The UI defaults to English. Use the **English / 한국어** selector to switch languages.
Your preference is saved per browser origin and synchronized across management tabs.
If storage is unavailable, switching still works for the current page. An automatically
assigned new port is a new origin and starts in English. Switching preserves form input
and running processes. Raw server/SSH error messages and user content are not translated.

Open the printed `api_url` in one or more ordinary local browser tabs/windows.
Use **＋ Workspace** to register an SSH host and named HTTP(S) bookmarks, with
optional descriptions. Mark a bookmark **Initial** to open it automatically on
this workspace’s first successful Chrome launch. It remains an ordinary bookmark
afterwards. **Open** uses Chrome’s native `--focus` option to activate a matching
URL in that workspace’s running Chrome, opening a new tab if none matches.
**Open all** processes each bookmark separately. The session, tunnel and reserved
port are preserved. Start Chrome first; opening a bookmark never starts a stopped
browser or SSH. Completion means Chrome accepted the command, not that the
page loaded.

Chrome determines URL matching; a redirected page may no longer match its saved
URL. Literal commas and trailing `*` have special selector meanings, so these
URLs open as ordinary new tabs. Older Chrome versions may also open duplicates.
Missing focus reports never trigger a retry, since the URL may already be open.
An explicit selector parse error is retried once without focus. No production
CDP connection or tab polling is used.

Bookmarks can be added, edited or removed while Chrome is running. Stop both
Chrome and SSH to change host, executable or session restoration settings.
**Start all** starts the tunnel and Chrome; individual controls remain available.
**Stop all** stops both and releases the port while preserving the profile.
After the first launch, session restoration opens the previous session; disabling
restoration opens a blank tab, without replaying Initial bookmarks.

Accepted requests appear as queued/running jobs, followed by completion, partial
success or failure in the shared activity list. Partial Start All retains a
healthy SSH tunnel if Chrome cannot start. Polling reads cached observations
without process-control side effects and remains responsive during commands.
Observations can lag a command; the shared activity list shows work in progress.
Recent runtime errors and unknown process states remain visible.

Closing the UI or stopping the Manager preserves running children. On Manager
restart, reload the management page (or open the new URL if using an automatic
port). Process/runtime state is rediscovered; job history is in memory for the
current Manager run, with at most 100 recent entries and 32 unfinished commands.
Graceful shutdown waits for an active command and cancels queued commands.

All API calls require `Authorization: Bearer TOKEN`. The same-origin HTML page
bootstraps a fresh token for each Manager run; tokens are not placed in URLs,
cookies or persistent runtime files. The exact loopback Host is checked on all
requests; any supplied Origin must match, and mutation requests require the
matching Origin plus JSON content type. No CORS grants or proxy-header trust are
enabled. HTML/assets/API responses use no-store and restrictive CSP/frame headers.
This protects against arbitrary web pages; trusted local programs can access
the HTML bootstrap. This is a single-user local service, not a remote deployment.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/state` | Manager, observed workspaces and recent jobs |
| GET | `/api/events` | Bearer-authenticated SSE state stream |
| GET | `/api/jobs/{id}` | One job's current state/outcome |
| POST | `/api/workspaces` | Register a version-1 workspace configuration |
| PUT | `/api/workspaces/{name}` | Edit bookmarks live; connection/browser edits require stopped processes |
| POST | `/api/workspaces/stop_all` | Stop the browser and tunnel of every workspace |
| POST | `/api/workspaces/{name}/workspace/{start_all,stop_all}` | Combined control |
| POST | `/api/workspaces/{name}/{tunnel,browser}/{start,stop,restart}` | Individual control |
| POST | `/api/workspaces/{name}/bookmarks/open` | Open all (`{}`), or one (`{"index": 0}`) bookmark |

Lifecycle commands use an empty `{}` JSON body. Bookmark indices refer to the current saved list.
Bookmark requests may only open saved HTTP(S) URLs; arbitrary request URLs are rejected. Mutations return **202 Accepted**
with a job and `Location` URL; this means queued, not successful execution. Poll
until `succeeded`, `partial`, `failed` or `cancelled`. Invalid requests return
400/413/415, unauthorized requests 401/403, a full queue 429. Errors during
execution are recorded on the accepted job. Bodies are limited to 16 KiB.

Real HTTP/SSH/Chrome verification against the running Docker fixture:

```sh
mise exec -- python experiments/api_probe.py --chrome
```

This starts isolated Managers, checks concurrent clients, shutdown/SIGKILL and
rediscovery, token rotation, partial success, and cleanup in `.m4-runs/`.

## Accessibility of the theme controls

The header controls for appearance mode, color scheme and UI style share one
hand-written dropdown component (`createDropdown` in `app.js`) instead of
native `<select>` elements, so all three look identical across every theme and
style variant. State lives in `theme.js` (mode, per-appearance scheme, style)
as the single source of truth; the view reads it on each render and writes back
through callbacks, so there is no second copy to synchronize.

Why not "native `<select>` + JS enhancement":

- That pattern's classic benefit is progressive enhancement — a usable control
  when JS fails. It does not apply here: the management UI already requires JS,
  so a no-JS fallback would do nothing.
- Its data layer is cheap: with the native select as the model and the custom
  UI as view/controller that only reads and writes it, no two-way
  synchronization is needed.
- Its accessibility cost is not specific to "replacement": once a native select
  is hidden, any *custom* control owns the keyboard and screen-reader behavior.
  The cost belongs to "custom", not to "replacement".

Currently supported: ARIA listbox roles/state plus keyboard operation (arrow
keys, Home/End, Enter/Space, Escape, Tab, click-outside, type-ahead) — not a full
native-select reimplementation (e.g. no mobile OS picker).

If screen-reader reachability becomes a requirement, the cheapest options are
to keep a visually hidden native `<select>` as the model and expose it to
assistive technology (custom view `aria-hidden`), or to add an explicit "use
native controls" preference. Automatic screen-reader detection is unreliable,
so an opt-in is the honest choice. A simple exposure policy for phones would be
custom on `pointer:fine` and native on `pointer:coarse`, switched by a media
query so only one control is ever in the tab order or the accessibility tree.

**Custom controls must still meet accessibility requirements, and this has not
been verified.** The component follows the ARIA patterns described above, but a
dedicated accessibility review (keyboard, screen reader, high contrast) is still
required; treat it as unverified until that is done.

## SSH tunnels

Prepare key/ssh-agent authentication and verify the host key with an ordinary
terminal SSH connection first. DevHard uses noninteractive authentication and
does not disable host-key checks. Then, with the foreground Manager stopped:

```sh
mise run devhard -- --data-dir .devhard tunnel start dev-a
mise run devhard -- --data-dir .devhard tunnel status dev-a
mise run devhard -- --data-dir .devhard tunnel restart dev-a
mise run devhard -- --data-dir .devhard tunnel stop dev-a
mise run devhard -- --data-dir .devhard workspace stop-all dev-a
```

Each control command owns the Manager lock for its duration. The SSH process
is detached and survives command/Manager exit. A subsequent command rediscovers
it using the OS start identity, executable, full command and unique launch marker.
macOS uses microsecond process start time; Linux uses boot ID and start ticks.
Stop sends SIGTERM only after ownership checks; it does not force-kill unknown
processes or process groups.

The default random allocation range is 60000–60999. Reservations are persisted
before launch and kept across individual stop/restart, failed authentication,
and Manager restart. A reserved-port conflict is reported without changing the
number. Only initial bind races retry up to three candidates. `workspace stop-all`
stops the browser and SSH before releasing the reservation. Unverified browser
ownership prevents this operation from releasing the port or stopping SSH.

Status separates process presence, owned listening socket and SOCKS response.
`listening` requires all three. It does **not** verify the remote server identity
or a particular application: `identity_verified` remains false.

Errors are returned as JSON `{code, message}` on stderr and saved as `last_error`
in runtime. In particular:

| Code | Next step |
| --- | --- |
| `authentication_failed` | Prepare keys/ssh-agent and test terminal SSH |
| `host_key_verification`, `host_key_changed` | Verify the host key in a terminal; do not bypass the check |
| `dns_failed`, `connection_failed`, `connection_timeout` | Check the alias, DNS/VPN and server reachability |
| `port_conflict` | Free the reserved port or explicitly stop all before allocating anew |
| `port_range_exhausted` | Free a reservation or adjust `[ports]` |
| `ownership_unknown`, `inspection_failed` | Inspect saved records and OS processes; no unverified process is stopped |
| `launch_uncertain` | Inspect the interrupted launch journal and processes before clearing it |

Each launch writes `runtime/tunnel-launch.json` before spawning and logs to
`runtime/tunnel.log`. After an interrupted Manager, a single verified process
matching the journal can be adopted. If no unique process can be established,
the journal remains and duplicate launch/control is blocked. Do not remove it
until the matching process is known to have exited. Logs are append-only in M2;
rotation is part of deployment preparation.

Configuration is versioned TOML; runtime records are versioned JSON. Every
workspace has a fixed `chrome-profile` directory. Managed child paths cannot be
symlinks. A symlink used explicitly as the data root resolves to the same lock.

```text
~/.devhard/
├── config.toml
├── runtime/
│   ├── manager.lock         # persistent inode; OS lock determines liveness
│   └── manager.json         # last run; may be stale after SIGKILL
└── workspaces/dev-a/
    ├── config.toml
    ├── chrome-profile/
    └── runtime/state.json
```

Initial global settings:

```toml
schema_version = 1

[ports]
min = 60000
max = 60999
```

Workspace configuration:

```toml
schema_version = 1
name = "dev-a"

[ssh]
host = "dev-a"

[browser]
profile = "chrome-profile"
executable = "auto"
restore_session = true

[[bookmarks]]
name = "Development app"
url = "http://127.0.0.1:3000"
description = "Main development UI"
initial = true

[[bookmarks]]
name = "API docs"
url = "http://127.0.0.1:6767"
description = "Open when needed"
initial = false
```

Legacy `tabs = ["http://..."]` configurations remain readable: each URL becomes
an Initial bookmark whose name is the URL. Saving writes canonical `bookmarks`.
Do not specify both fields. Empty collections serialize as `bookmarks = []`.

Manager startup validates and reads all configurations and runtime records
before publishing `ready`. Invalid/missing runtime data and duplicate port
reservations block startup without erasing the records. SSH ownership and socket
checks determine tunnel state; browser ownership includes its exact profile,
proxy port and unique launch marker. Legacy/unverified records remain `unknown`.
Manager exit preserves profiles,
reservations and process records. Do not delete an active lock file.

## M0 lifecycle experiment

Requires Python 3.13+, macOS or Linux, and `ps`. The basic probe uses a disposable
worker; `--chrome` adds real Chrome and `--ssh-host` adds a real SSH tunnel.
Every run uses its own data directory under `.m0-runs/`.

```sh
mise exec -- python experiments/lifecycle_probe.py
mise exec -- python experiments/lifecycle_probe.py --chrome
mise exec -- python experiments/lifecycle_probe.py --chrome --ssh-host dev-a --http-url http://127.0.0.1:3000
```

The probe checks duplicate-manager rejection, normal and forced manager exit,
child survival, lock reacquisition, process rediscovery, Chrome profile reuse,
and owned-process cleanup. Evidence is written to each run's `result.json`.
Repeat `--http-url` to check multiple services through the same tunnel. SOCKS
and HTTP checks run before manager exit, while the manager is stopped after
both normal and forced exits, and after rediscovery.
The probe is experimental code, not the public DevHard CLI.

## M2 real-server verification

```sh
mise exec -- python experiments/tunnel_probe.py --ssh-host dev-a \
  --http-url http://127.0.0.1:3000 --http-url http://127.0.0.1:6767
```

This uses the product controllers and CLI with a disposable `.m2-runs/` data
root. It verifies independent reservations, adoption, restarts, SIGKILL recovery,
real conflicts, HTTP over SOCKS and cleanup. It also checks DNS error reporting
using a nonexistent `.invalid` hostname. No remote server files are created.

## Chrome and workspace lifecycle

```sh
mise run devhard -- --data-dir .devhard workspace start-all dev-a
mise run devhard -- --data-dir .devhard browser status dev-a
mise run devhard -- --data-dir .devhard browser stop dev-a
mise run devhard -- --data-dir .devhard browser start dev-a
mise run devhard -- --data-dir .devhard browser restart dev-a
mise run devhard -- --data-dir .devhard workspace stop-all dev-a
```

`start-all` starts the tunnel first. If the browser fails, SSH remains running;
the command prints a JSON `result: partial` with the error and current workspace
snapshot and exits with code 1. A successful full operation returns `success`.
Snapshot `config` contains configuration; `tunnel` and `browser` contain observed
state. Control commands currently require the foreground Manager to be stopped.

Browser and tunnel controls are independent. Starting the browser alone reserves
a shared port but does not start SSH; pages can connect once the tunnel starts.
Stopping either component preserves the other and the port. Manager exit also
leaves both running. Workspace Stop All preserves the entire browser profile.

Chrome/Chromium is discovered in the usual macOS application directories. An
explicit `browser.executable` must be an absolute path to a native executable,
not an `.app` directory or shell wrapper. Linux discovery is implemented but
awaits M5 verification. Chrome is launched directly with its own `--user-data-dir`,
SOCKS proxy and loopback bypass override. Normal launches do not enable CDP or
remote debugging.

Initial bookmarks are used only for the first successful launch. Later launches
with `restore_session = true` use Chrome’s `--restore-last-session`; with false,
DevHard passes `about:blank`. Runtime `browser_initialized` remembers this policy
even after Stop All. Editing Initial flags never resets it. Chrome’s own startup
and crash-recovery preferences are not overridden. Session contents and recovery
after corruption remain Chrome’s responsibility; DevHard does not back up tabs.

Opening bookmarks uses native Chrome command forwarding for the same profile;
production launches do not enable CDP. A write-ahead notification journal protects
Manager interruption and browser-exit races. A replacement browser created during
such a race is cleaned up if uniquely verified, never silently adopted. Interrupted
requests are not replayed automatically, since they may already have opened tabs.

Before starting or stopping Chrome, DevHard checks profile users as well as saved
process identity. The profile lock also protects relative/symlink aliases. An
unmanaged main process using the profile is reported as
`unknown`; DevHard neither adopts it nor sends it signals. Duplicate Start for an
owned running browser returns its status without reopening tabs. No window
activation feature is implemented yet.

`browser-launch.json` is written before spawn to cover Manager death before PID
publication. A uniquely verified matching launch can be recovered at startup.
Ambiguous journals block control and must be inspected before being cleared.
Chrome's internal profile lock files are not deleted by DevHard. Stop
requests normal app termination on macOS and waits for verified main-process exit.
A rejected normal quit request is not replaced with a force quit. Other platforms
and non-GUI test processes use SIGTERM.
Changing an active SSH alias or reserved forwarding port is reported as unknown;
explicitly stop/restart the owned tunnel to apply the change.

## M3 real-browser verification

```sh
mise exec -- python experiments/browser_probe.py --ssh-host dev-a --second-ssh-host dev-b \
  --http-url http://127.0.0.1:3000 --http-url http://127.0.0.1:6767
```

Both test servers must serve the given URLs. This opens disposable visible Chrome
profiles in `.m3-runs/`. CDP is enabled only for those test profiles to verify
actual document HTTP status and tab restoration. The probe covers concurrent
Start, independent controls, Manager SIGKILL, a Chrome crash/restart, launch-journal
recovery, stale identity rejection, an unmanaged profile, partial Start All and
cleanup. It does not add a production CDP dependency or remote server files.

## Disposable Docker test servers

The [SSH/web fixture](tests/fixtures/ssh-web/README.md) builds one image and starts
two independent SSH endpoints, each serving loopback HTTP apps on 3000 and 6767.
With the dedicated Colima profile running:

```sh
mise run fixture -- up --context colima-devhard-tests
mise run fixture -- check
mise run fixture -- probe --chrome
mise run fixture -- down
```

Generated keys and pinned host keys use a private test configuration; the user's
SSH files are unchanged. Optional global `[ssh] config_file = "/absolute/path"`
selects a separate OpenSSH configuration for a DevHard data directory.

## Bookmark verification

```sh
mise exec -- python experiments/bookmark_probe.py
```

This disposable real-Chrome probe checks Initial-only startup, individual/all
bookmark opening, existing-tab activation, mixed focus/open requests, literal selector
characters, Unicode URLs, the same native process and reservation,
live metadata edits, blank later launches without restoration, and exit-race cleanup.
CDP is used only by the probe to observe test tabs.

The management UI receives SSE through a Bearer-authenticated fetch stream: a full
snapshot on connection, then the latest snapshot when state or jobs change.
Heartbeats run every 15 seconds; interrupted connections retry automatically.
Refresh the page after a Manager restart to obtain its new token. Internal process
observation remains periodic. `/api/state` remains available for one-off reads.
