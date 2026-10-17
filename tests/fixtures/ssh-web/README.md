# Disposable SSH and web servers

One image runs OpenSSH and two HTTP apps bound to `127.0.0.1:3000` and
`127.0.0.1:6767` in the same container. The runner creates two containers with
distinct `fixture-a` / `fixture-b` identities, letting both use identical URLs.
`/identity` returns JSON; other paths return an HTML page with the same identity.

From the repository root:

```sh
# macOS: isolated VM, preserving the current Docker context and SSH config
colima start --profile devhard-tests --cpus 2 --memory 2 --disk 10 \
  --activate=false --ssh-config=false --mount "$PWD:w"
mise run fixture -- up
mise run fixture -- check
mise run fixture -- probe --chrome
mise run fixture -- status
mise run fixture -- down
colima stop --profile devhard-tests
```

An existing Docker engine can be selected with `up --context CONTEXT_NAME`.
Subsequent commands use the saved context. `probe` runs the M2 SSH lifecycle
checks; `--chrome` also runs M3 with real visible native Chrome windows and
asserts the server identity inside loaded documents. Chrome remains on the host.
Run fixture lifecycle commands one at a time from one checkout.

Only SSH is published, on random host loopback ports. HTTP ports are neither
published nor bound to the container network interface. `check` verifies this
and retrieves all four identities through DevHard's actual SSH SOCKS tunnels.
It does not assume host ports 3000 or 6767 are unused.

Every run generates a client key and independent server host keys. The image
contains no usable keys. Runtime keys are mounted read-only, and client private
keys stay on the host. Authentication uses only the generated public key;
forwarding is restricted to the two app addresses. A dedicated SSH configuration
pins the generated host public keys with strict checking. Nothing is added to
the user's `~/.ssh/config` or `known_hosts`.

State, keys and check evidence live under ignored `.fixture-runs/` (run
directories are mode 0700, private keys mode 0600). `status` shows the generated
SSH configuration and ports. For manual access:

```sh
ssh -F /absolute/path/from/status/ssh_config devhard-fixture-a
```

`down` deletes only recorded containers after checking their run label. It
retains the ignored runtime directory for diagnosis, including disposable test
keys; remove that run directory when no longer needed. `up` refuses to overwrite
an active run. For interruption during startup, use `down` before retrying.
Containers stay running after checks, while probe Chrome/SSH processes are
cleaned up by the probes. Colima is stopped separately when finished.

An isolated DevHard data directory can select the same SSH file in `config.toml`:

```toml
schema_version = 1
[ssh]
config_file = "/absolute/path/from/status/ssh_config"
```

The optional absolute path becomes one `ssh -F` argument. Omitting it retains
normal OpenSSH configuration lookup. Changing the path of a live tunnel requires
an explicit stop/restart; editing its contents also takes effect on the next
SSH launch. This fixture does not validate Linux Manager/Chrome or WebSocket/HMR.
