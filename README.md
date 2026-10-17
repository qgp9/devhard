# DevHard

Workspace launcher for independent Chrome profiles and SSH SOCKS5 tunnels.

M1 implements the data store, workspace registry, and foreground Manager lock.
Product requirements and development records live in the sibling `../notes/`
directory. Tunnel/browser controls and the HTTP API arrive in later milestones.

## Development

Python is pinned to **3.13.16** by `mise.toml`. M1 uses only the standard library.

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
reservations block startup without erasing the records. A saved process record
is reported as `unknown` until process ownership checks arrive in M2/M3; M1
does not signal processes or allocate ports. Manager exit preserves profiles,
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
