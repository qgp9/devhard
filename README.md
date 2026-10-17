# DevHard

Workspace launcher for independent Chrome profiles and SSH SOCKS5 tunnels.

Implementation is starting with lifecycle experiments. Product requirements and
development records live in the sibling `../notes/` directory.

## M0 lifecycle experiment

Requires Python 3.13+, macOS or Linux, and `ps`. The basic probe uses a disposable
worker; `--chrome` adds real Chrome and `--ssh-host` adds a real SSH tunnel.
Every run uses its own data directory under `.m0-runs/`.

```sh
python3 experiments/lifecycle_probe.py
python3 experiments/lifecycle_probe.py --chrome
python3 experiments/lifecycle_probe.py --chrome --ssh-host dev-a --http-url http://127.0.0.1:3000
python3 -m unittest discover -s tests -v
```

The probe checks duplicate-manager rejection, normal and forced manager exit,
child survival, lock reacquisition, process rediscovery, Chrome profile reuse,
and owned-process cleanup. Evidence is written to each run's `result.json`.
Repeat `--http-url` to check multiple services through the same tunnel. SOCKS
and HTTP checks run before manager exit, while the manager is stopped after
both normal and forced exits, and after rediscovery.
The probe is experimental code, not the public DevHard CLI.
