# DevHard

Workspace launcher for independent Chrome profiles and SSH SOCKS5 tunnels.

M3 implements the data store, workspace registry, Manager lock, port reservations,
independent SSH/Chrome controls and workspace Start All/Stop All.
Product requirements and development records live in the sibling `../notes/`
directory. The HTTP API and management UI arrive in M4.

## Development

Python is pinned to **3.13.16** by `mise.toml`. Python code uses only the standard
library. Tunnel controls require OpenSSH, `ps`, and `lsof`; macOS process identity
uses system libproc and normal application quit uses AppKit through ctypes. Linux execution is scheduled for M5.

```sh
mise trust
mise install
mise run test
mise run devhard -- --help
```

Use the same `--data-dir` before the subcommand for every invocation; omit it to
use `~/.devhard`. Workspace names use lowercase letters, digits, `_` and `-`.

```sh
mise run devhard -- --data-dir .devhard workspace add dev-a --ssh-host dev-a --tab http://127.0.0.1:3000
mise run devhard -- --data-dir .devhard workspace list
mise run devhard -- --data-dir .devhard workspace show dev-a
mise run devhard -- --data-dir .devhard manager run
# In another terminal:
mise run devhard -- --data-dir .devhard manager status
```

`manager run` prints a JSON ready snapshot and stays in the foreground until
Ctrl-C or SIGTERM. A second Manager for the same physical data directory exits
with code 73. `workspace add` takes the same lock and currently requires the
foreground Manager to be stopped; read-only list/show/status work while it runs.
The HTTP client path for mutations will be added with the API.

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
tabs = ["http://127.0.0.1:3000"]

[ssh]
host = "dev-a"

[browser]
profile = "chrome-profile"
executable = "auto"
restore_session = true
```

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

Initial URLs are used for the first successful launch. Later launches with
`restore_session = true` use Chrome's `--restore-last-session` without readding
initial URLs. Runtime `browser_initialized` remembers this policy even after
Stop All; it defaults to false for older records. With `restore_session = false`,
configured initial URLs are passed on every launch. Session contents and recovery
after corruption remain Chrome's responsibility; DevHard does not back up tabs.

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
mise run fixture -- up
mise run fixture -- check
mise run fixture -- probe --chrome
mise run fixture -- down
```

Generated keys and pinned host keys use a private test configuration; the user's
SSH files are unchanged. Optional global `[ssh] config_file = "/absolute/path"`
selects a separate OpenSSH configuration for a DevHard data directory.
