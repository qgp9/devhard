# DevHard

Workspace launcher for independent Chrome profiles and SSH SOCKS5 tunnels.

M2 implements the data store, workspace registry, Manager lock, port reservations,
and SSH tunnel controls.
Product requirements and development records live in the sibling `../notes/`
directory. Browser controls and the HTTP API arrive in later milestones.

## Development

Python is pinned to **3.13.16** by `mise.toml`. Python code uses only the standard
library. Tunnel controls require OpenSSH, `ps`, and `lsof`; macOS process identity
also uses the system libproc library. Linux execution is scheduled for M5.

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
releases the reservation after stopping SSH; M2 refuses this operation if a
browser process record exists, since browser control arrives in M3.

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
checks determine tunnel state; browser records remain `unknown` until M3.
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
