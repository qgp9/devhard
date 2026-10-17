"""M0: detached processes, OS lock, rediscovery, and optional real SSH/Chrome."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid


SCRIPT = Path(__file__).resolve()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def snapshot(pid: int) -> dict | None:
    result = subprocess.run(
        ['ps', '-ww', '-p', str(pid), '-o', 'lstart=', '-o', 'stat=', '-o', 'command='],
        capture_output=True, text=True, check=False,
    )
    parts = result.stdout.strip().split(None, 6)
    if result.returncode or len(parts) < 7 or parts[5].startswith('Z'):
        return None
    return {'pid': pid, 'started': ' '.join(parts[:5]), 'command': parts[6]}


def owned(record: dict) -> bool:
    current = snapshot(record['pid'])
    return current == {key: record[key] for key in ('pid', 'started', 'command')} and record['marker'] in current['command']


def spawn_detached(root: Path, name: str, argv: list[str], marker: str) -> dict:
    with (root / f'{name}.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
    try:
        state = snapshot(child.pid)
        if state is None or marker not in state['command']:
            raise RuntimeError(f'{name}: cannot establish ownership')
    except Exception:
        child.terminate()
        child.wait(timeout=5)
        raise
    return {**state, 'marker': marker, 'name': name}


def wait_until(predicate, timeout: float = 10) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.1)
    raise TimeoutError('condition was not satisfied')


def socks_ready(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1) as stream:
            stream.sendall(b'\x05\x01\x00')
            response = b''
            while len(response) < 2:
                piece = stream.recv(2 - len(response))
                if not piece:
                    return False
                response += piece
            return response == b'\x05\x00'
    except OSError:
        return False


def wait_tunnel(record: dict, log_path: Path) -> None:
    def ready():
        if not owned(record):
            detail = log_path.read_text(errors='replace')[-2000:].strip()
            raise RuntimeError(f'SSH exited or ownership changed: {detail}')
        return socks_ready(record['port'])

    wait_until(ready, timeout=15)


def terminate_owned(record: dict) -> None:
    if not owned(record):
        if snapshot(record['pid']) is not None:
            raise RuntimeError(f"refusing to terminate unverified process {record['pid']}")
        return
    # This experiment launches each child as a new session/process-group leader.
    if os.getpgid(record['pid']) != record['pid']:
        raise RuntimeError('unexpected process group')
    os.killpg(record['pid'], signal.SIGTERM)
    wait_until(lambda: not owned(record))


def manager(root: Path, launch: bool, chrome: str | None, host: str | None) -> int:
    with (root / 'manager.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 73
        records_path = root / 'children.json'
        records = json.loads(records_path.read_text()) if records_path.exists() else {}
        if launch:
            marker = str(uuid.uuid4())
            records['worker'] = spawn_detached(root, 'worker', [sys.executable, str(SCRIPT), 'worker', marker], marker)
            write_json(records_path, records)
            if host:
                # Reserve one candidate only for this experiment. SSH binding is authoritative.
                with socket.socket() as candidate:
                    candidate.bind(('127.0.0.1', 0))
                    port = candidate.getsockname()[1]
                marker = f'127.0.0.1:{port}'
                records['ssh'] = spawn_detached(root, 'ssh', [
                    'ssh', '-N', '-S', 'none', '-o', 'BatchMode=yes',
                    '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
                    '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                    '-D', marker, host,
                ], marker)
                records['ssh']['port'] = port
                write_json(records_path, records)
                wait_tunnel(records['ssh'], root / 'ssh.log')
            if chrome:
                profile = str(root / 'chrome-profile')
                port = records.get('ssh', {}).get('port', 60999)
                records['chrome'] = spawn_detached(root, 'chrome', [
                    chrome, f'--user-data-dir={profile}', '--no-first-run',
                    '--no-default-browser-check', f'--proxy-server=socks5://127.0.0.1:{port}',
                    '--proxy-bypass-list=<-loopback>', 'about:blank',
                ], profile)
                write_json(records_path, records)
                time.sleep(2)
        found = {name: owned(record) for name, record in records.items()}
        write_json(root / 'manager.json', {'pid': os.getpid(), 'discovered': found})
        if not found or not all(found.values()):
            return 74
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            time.sleep(.2)


def run_probe(args) -> None:
    base = SCRIPT.parent.parent / '.m0-runs'
    base.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='probe-', dir=base))
    evidence = {'root': str(root), 'checks': [], 'platform': sys.platform,
                'python': sys.version, 'ssh_requested': bool(args.ssh_host), 'chrome_requested': args.chrome}
    processes = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        evidence['checks'].append(label)
        print(f'PASS {label}', flush=True)

    def check_tunnel(records, phase):
        if not args.ssh_host:
            return
        port = records['ssh']['port']
        check(f'SOCKS handshake {phase}', socks_ready(port))
        for url in args.http_url:
            response = subprocess.run([
                'curl', '--noproxy', '', '--proxy', f'socks5h://127.0.0.1:{port}',
                '--fail', '--silent', '--show-error', '--max-time', '15',
                '--output', os.devnull, '--write-out', '%{http_code}', url,
            ], capture_output=True, text=True, timeout=20)
            evidence.setdefault('http_results', []).append({
                'phase': phase, 'url': url, 'status': response.stdout,
                'returncode': response.returncode, 'error': response.stderr[-1000:],
            })
            check(f'remote HTTP {url} {phase}', response.returncode == 0)

    chrome = None
    if args.chrome:
        chrome = args.chrome_executable or ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
                    if sys.platform == 'darwin' else shutil.which('google-chrome') or shutil.which('chromium'))
        if not chrome or not Path(chrome).is_file():
            raise RuntimeError('Chrome executable not found')

    def start(launch=False):
        state_path = root / 'manager.json'
        state_path.unlink(missing_ok=True)
        argv = [sys.executable, str(SCRIPT), 'manager', str(root)]
        if launch:
            argv += ['--launch']
        if chrome:
            argv += ['--chrome-executable', chrome]
        if args.ssh_host:
            argv += ['--ssh-host', args.ssh_host]
        with (root / 'manager.log').open('ab', buffering=0) as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
        processes.append(proc)
        wait_until(lambda: state_path.exists() or proc.poll() is not None, timeout=25)
        if proc.poll() is not None:
            detail = (root / 'manager.log').read_text(errors='replace')[-3000:].strip()
            raise RuntimeError(f'Manager exited during startup ({proc.returncode}): {detail}')
        check('manager ready' if launch else 'manager reacquired lock', proc.poll() is None and state_path.exists())
        state = json.loads(state_path.read_text())
        check('all children discovered', all(state['discovered'].values()))
        return proc

    try:
        first = start(launch=True)
        duplicate = subprocess.run([sys.executable, str(SCRIPT), 'manager', str(root)], timeout=5)
        check('duplicate manager rejected', duplicate.returncode == 73)
        records = json.loads((root / 'children.json').read_text())
        check_tunnel(records, 'before manager exit')
        first.terminate()
        check('manager graceful exit', first.wait(timeout=5) == 0)
        check('children survive graceful exit', all(owned(r) for r in records.values()))
        check_tunnel(records, 'after graceful exit')
        second = start()
        second.kill()
        check('manager forced exit', second.wait(timeout=5) == -signal.SIGKILL)
        check('children survive forced exit', all(owned(r) for r in records.values()))
        check_tunnel(records, 'after forced exit')
        third = start()
        check('children did not inherit manager lock', third.poll() is None)
        if chrome:
            repeated = subprocess.run([
                chrome, f'--user-data-dir={root / "chrome-profile"}', 'about:blank',
            ], stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
            check('Chrome second launch returned to existing profile', repeated.returncode == 0 and owned(records['chrome']))
            evidence['chrome_second_launch'] = repeated.stdout.decode(errors='replace')[-500:]
        check_tunnel(records, 'after manager restart')
        third.terminate()
        third.wait(timeout=5)
        evidence['children'] = records
        evidence['success'] = True
    except Exception as error:
        evidence['success'] = False
        evidence['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        cleanup_errors = []
        if (root / 'children.json').exists():
            for record in json.loads((root / 'children.json').read_text()).values():
                try:
                    terminate_owned(record)
                except Exception as error:
                    cleanup_errors.append(str(error))
        evidence['cleanup_errors'] = cleanup_errors
        if cleanup_errors:
            evidence['success'] = False
        write_json(root / 'result.json', evidence)
        print(f'Evidence: {root / "result.json"}', flush=True)
        if cleanup_errors:
            raise RuntimeError(f'cleanup incomplete: {cleanup_errors}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', action='store_true')
    parser.add_argument('--chrome-executable')
    parser.add_argument('--ssh-host')
    parser.add_argument('--http-url', action='append', default=[], help='Repeat for multiple remote HTTP services')
    parser.add_argument('mode', nargs='?', default='probe', choices=['probe', 'manager', 'worker'])
    parser.add_argument('data', nargs='?')
    parser.add_argument('--launch', action='store_true')
    args = parser.parse_args()
    if args.mode == 'worker':
        while True:
            time.sleep(1)
    elif args.mode == 'manager':
        sys.exit(manager(Path(args.data), args.launch, args.chrome_executable, args.ssh_host))
    else:
        if args.http_url and not args.ssh_host:
            parser.error('--http-url requires --ssh-host')
        run_probe(args)


if __name__ == '__main__':
    main()
