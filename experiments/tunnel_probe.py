"""Exercise M2 product controllers against an explicitly supplied real SSH host."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text
from devhard.tunnel import TunnelController, TunnelError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', required=True)
    parser.add_argument('--http-url', action='append', required=True)
    parser.add_argument('--ssh-config', type=Path)
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1] / '.m2-runs'
    base.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='probe-', dir=base))
    store = Store(root)
    if args.ssh_config:
        atomic_write(root / 'config.toml', 'schema_version = 1\n[ssh]\nconfig_file = '
                     + json.dumps(str(args.ssh_config.resolve())) + '\n')
    result = {'root': str(root), 'checks': [], 'http_results': []}
    foreground = None

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        result['checks'].append(label)
        print(f'PASS {label}', flush=True)

    def http(port, phase):
        for url in args.http_url:
            request = subprocess.run(['curl', '--noproxy', '', '--proxy', f'socks5h://127.0.0.1:{port}',
                '--fail', '--silent', '--show-error', '--max-time', '15', '--output', os.devnull,
                '--write-out', '%{http_code}', url], capture_output=True, text=True, timeout=20)
            result['http_results'].append({'url': url, 'phase': phase, 'status': request.stdout,
                                          'error': request.stderr, 'returncode': request.returncode})
            check(f'{phase}: {url}', request.returncode == 0)

    try:
        with Manager(store) as manager:
            for name in ('dev-a', 'dev-b'):
                manager.register(WorkspaceConfig(name, args.ssh_host, tuple(args.http_url)))
            first = manager.tunnel_action('dev-a', 'start')
            check('initial SSH listener verified', first['state'] == 'listening' and first['listener_owned'])
            port = first['port']
            pid = store.runtime('dev-a').processes['tunnel']['pid']
            check('port is inside configured range', 60000 <= port <= 60999)
            same = manager.tunnel_action('dev-a', 'start')
            check('duplicate start is idempotent', same['port'] == port and store.runtime('dev-a').processes['tunnel']['pid'] == pid)
        http(port, 'after Manager context exit')
        cli = [sys.executable, '-m', 'devhard', '--data-dir', str(root)]
        queried = subprocess.run(cli + ['tunnel', 'status', 'dev-a'], capture_output=True, text=True, timeout=10)
        check('CLI status reports owned listener', queried.returncode == 0 and json.loads(queried.stdout)['state'] == 'listening')
        repeated = subprocess.run(cli + ['tunnel', 'start', 'dev-a'], capture_output=True, text=True, timeout=10)
        check('CLI start adopts existing tunnel', repeated.returncode == 0 and json.loads(repeated.stdout)['port'] == port
              and store.runtime('dev-a').processes['tunnel']['pid'] == pid)
        with Manager(store) as manager:
            adopted = manager.tunnels.observe('dev-a')
            check('new Manager rediscovers same SSH PID', adopted['state'] == 'listening' and store.runtime('dev-a').processes['tunnel']['pid'] == pid)
            other = manager.tunnel_action('dev-b', 'start')
            check('workspaces use distinct reserved ports', other['state'] == 'listening' and other['port'] != port)
            manager.tunnel_action('dev-b', 'stop_all')
            check('Stop All releases second reservation', store.runtime('dev-b').reserved_port is None)
            restarted = manager.tunnel_action('dev-a', 'restart')
            check('tunnel restart keeps reservation', restarted['state'] == 'listening' and restarted['port'] == port)
            check('restart replaces SSH PID', store.runtime('dev-a').processes['tunnel']['pid'] != pid)
        http(port, 'after tunnel restart')
        # A real foreground Manager dies; the SSH process was started by an earlier Manager.
        with (root / 'foreground.log').open('wb') as log:
            foreground = subprocess.Popen([sys.executable, '-m', 'devhard', '--data-dir', str(root), 'manager', 'run'],
                                           stdout=log, stderr=log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = json.loads((root / 'runtime' / 'manager.json').read_text())
            if info['pid'] == foreground.pid and info['phase'] == 'ready':
                break
            if foreground.poll() is not None:
                raise RuntimeError('foreground Manager exited before ready')
            time.sleep(.05)
        else:
            raise TimeoutError('foreground Manager startup')
        foreground.kill()
        check('foreground Manager forced exit', foreground.wait(timeout=5) == -signal.SIGKILL)
        http(port, 'after Manager SIGKILL')
        with Manager(store) as manager:
            check('rediscovery after SIGKILL', manager.tunnels.observe('dev-a')['state'] == 'listening')
            manager.tunnel_action('dev-a', 'stop')
            check('individual stop retains reservation', store.runtime('dev-a').reserved_port == port)
            with socket.socket() as conflict:
                conflict.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                conflict.bind(('127.0.0.1', port))
                conflict.listen()
                try:
                    manager.tunnel_action('dev-a', 'start')
                    raise AssertionError('occupied reserved port was accepted')
                except TunnelError as error:
                    check('reserved port conflict is explicit', error.code == 'port_conflict')
                check('conflict does not reallocate', store.runtime('dev-a').reserved_port == port)
            resumed = manager.tunnel_action('dev-a', 'start')
            check('same reservation works after conflict clears', resumed['state'] == 'listening' and resumed['port'] == port)
            profile = store.safe('workspaces', 'dev-a', 'chrome-profile', 'probe-marker')
            profile.write_text('preserve me')
            manager.tunnel_action('dev-a', 'stop_all')
            check('Stop All releases first reservation', store.runtime('dev-a').reserved_port is None)
            check('Stop All preserves profile', profile.read_text() == 'preserve me')
            manager.register(WorkspaceConfig('dns-failure', 'devhard-nonexistent.invalid'))
        failed = subprocess.run(cli + ['tunnel', 'start', 'dns-failure'], capture_output=True, text=True, timeout=25)
        check('CLI DNS failure is classified', failed.returncode == 1 and json.loads(failed.stderr)['code'] == 'dns_failed')
        check('failed SSH launch leaves no process or journal',
              store.runtime('dns-failure').processes['tunnel'] is None
              and not TunnelController(store).journal_path('dns-failure').exists())
        result['success'] = True
    except BaseException as error:
        result['success'] = False
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if foreground is not None and foreground.poll() is None:
            foreground.terminate()
            foreground.wait(timeout=5)
        cleanup_errors = []
        try:
            with Manager(store) as manager:
                for config in store.workspaces():
                    try:
                        manager.tunnel_action(config.name, 'stop_all')
                    except Exception as error:
                        cleanup_errors.append(str(error))
        except Exception as error:
            cleanup_errors.append(str(error))
        result['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            result['success'] = False
        atomic_write(root / 'result.json', json_text(result))
        print(f'Evidence: {root / "result.json"}', flush=True)
        if cleanup_errors:
            raise RuntimeError(f'cleanup incomplete: {cleanup_errors}')


if __name__ == '__main__':
    main()
