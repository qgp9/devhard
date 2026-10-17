"""M3 real Chrome + SSH verification. CDP is enabled only in disposable test profiles."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, quote
from urllib.request import build_opener, ProxyHandler, Request
import uuid

from devhard import process
from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text
from devhard.tunnel import TunnelError


OPENER = build_opener(ProxyHandler({}))


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise TimeoutError('browser verification condition')


def targets(profile):
    port = int((profile / 'DevToolsActivePort').read_text().splitlines()[0])
    with OPENER.open(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
        return [entry for entry in json.load(response) if entry['type'] == 'page']


def cdp(url, method, params=None):
    """Small test-only WebSocket client for one CDP request; no product dependency."""
    parsed = urlsplit(url)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as stream:
        key = base64.b64encode(os.urandom(16)).decode()
        stream.sendall((f'GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n'
                        f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        reader = stream.makefile('rb')
        header = reader.readline()
        if b'101' not in header:
            raise RuntimeError('CDP WebSocket upgrade failed')
        headers = {}
        while (line := reader.readline()) not in (b'\r\n', b''):
            name, value = line.decode().split(':', 1)
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if headers.get('sec-websocket-accept') != expected:
            raise RuntimeError('invalid CDP WebSocket handshake')

        def send(payload, opcode=1):
            mask = os.urandom(4)
            length = len(payload)
            prefix = bytes([0x80 | opcode])
            prefix += bytes([0x80 | length]) if length < 126 else bytes([0x80 | 126]) + struct.pack('!H', length)
            stream.sendall(prefix + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

        send(json.dumps({'id': 1, 'method': method, 'params': params or {}}).encode())
        while True:
            head = reader.read(2)
            if len(head) != 2:
                raise RuntimeError('CDP socket closed')
            opcode, length = head[0] & 15, head[1] & 127
            if length == 126:
                length = struct.unpack('!H', reader.read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', reader.read(8))[0]
            if length > 2**20 or head[1] & 128:
                raise RuntimeError('unexpected CDP test frame')
            payload = reader.read(length)
            if opcode == 9:
                send(payload, 10)
            elif opcode == 8:
                raise RuntimeError('CDP closed before reply')
            elif opcode == 1:
                message = json.loads(payload)
                if message.get('id') == 1:
                    if 'error' in message:
                        raise RuntimeError(str(message['error']))
                    return message.get('result')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', required=True)
    parser.add_argument('--second-ssh-host', required=True)
    parser.add_argument('--http-url', action='append', required=True)
    parser.add_argument('--ssh-config', type=Path)
    parser.add_argument('--expected-first-id')
    parser.add_argument('--expected-second-id')
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1] / '.m3-runs'
    base.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='probe-', dir=base))
    store = Store(root)
    if args.ssh_config:
        atomic_write(root / 'config.toml', 'schema_version = 1\n[ssh]\nconfig_file = '
                     + json.dumps(str(args.ssh_config.resolve())) + '\n')
    evidence = {'root': str(root), 'checks': [], 'documents': []}
    foreground = None
    manual_children = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        evidence['checks'].append(label)
        print(f'PASS {label}', flush=True)

    def enable(manager):
        manager.browsers.extra_args = ('--remote-debugging-port=0',)

    def pages(name):
        return targets(store.safe('workspaces', name, 'chrome-profile'))

    def loaded(name):
        entries = pages(name)
        origins = {urlsplit(url).netloc for url in args.http_url}
        selected = [entry for entry in entries if urlsplit(entry['url']).netloc in origins]
        return selected if len(selected) == len(args.http_url) else None

    def documents(name):
        for entry in wait_for(lambda: loaded(name)):
            value = cdp(entry['webSocketDebuggerUrl'], 'Runtime.evaluate', {
                'expression': "JSON.stringify({url:location.href,status:performance.getEntriesByType('navigation')[0]?.responseStatus,error:document.documentURI.startsWith('chrome-error:'),fixture_id:document.documentElement.dataset.fixtureId})",
                'returnByValue': True})
            document = json.loads(value['result']['value'])
            evidence['documents'].append({'workspace': name, **document})
            check(f'{name}: Chrome loaded {urlsplit(document["url"]).netloc}', document['status'] == 200 and not document['error'])
            expected = args.expected_first_id if name == 'dev' else args.expected_second_id
            if expected:
                check(f'{name}: expected fixture identity', document.get('fixture_id') == expected)

    try:
        with Manager(store) as manager:
            enable(manager)
            for name, host in (('dev', args.ssh_host), ('ops', args.second_ssh_host)):
                manager.register(WorkspaceConfig(name, host, tuple(args.http_url)))
                result = manager.workspace_action(name, 'start_all')
                check(f'{name}: Start All', result['result'] == 'success')
            dev = store.runtime('dev')
            ops = store.runtime('ops')
            port = dev.reserved_port
            browser_pid = dev.processes['browser']['pid']
            check('separate profiles and proxy ports', port != ops.reserved_port and
                  dev.processes['browser']['pid'] != ops.processes['browser']['pid'])
            documents('dev')
            documents('ops')
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: manager.browser_action('dev', 'start'), range(2)))
            check('concurrent browser Start is idempotent', all(result['state'] == 'running' for result in results)
                  and store.runtime('dev').processes['browser']['pid'] == browser_pid)
            manager.tunnel_action('dev', 'restart')
            check('tunnel-only restart preserves Chrome and port', store.runtime('dev').reserved_port == port
                  and store.runtime('dev').processes['browser']['pid'] == browser_pid)
            for entry in pages('dev'):
                cdp(entry['webSocketDebuggerUrl'], 'Page.reload')
            time.sleep(1)
            documents('dev')
            manager.tunnel_action('dev', 'stop')
            check('tunnel Stop keeps Chrome', manager.browsers.observe('dev')['state'] == 'running')
            with socket.socket() as conflict:
                conflict.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                conflict.bind(('127.0.0.1', port))
                conflict.listen()
                try:
                    manager.tunnel_action('dev', 'start')
                    raise AssertionError('reserved port conflict accepted')
                except TunnelError as error:
                    check('conflict with live Chrome retains port', error.code == 'port_conflict' and store.runtime('dev').reserved_port == port)
            manager.tunnel_action('dev', 'start')
            # A committed document exercises session persistence; an untouched blank
            # tab can have no saved navigation entry and is unsuitable as evidence.
            debug_port = (manager.browsers.profile('dev') / 'DevToolsActivePort').read_text().splitlines()[0]
            extra_url = args.http_url[0].split('#', 1)[0] + '#devhard-session-probe'
            with OPENER.open(Request(f'http://127.0.0.1:{debug_port}/json/new?{quote(extra_url, safe="")}', method='PUT'), timeout=3):
                pass
            def committed():
                selected = [entry for entry in pages('dev') if urlsplit(entry['url']).fragment == 'devhard-session-probe']
                if not selected:
                    return False
                value = cdp(selected[0]['webSocketDebuggerUrl'], 'Runtime.evaluate', {
                    'expression': "performance.getEntriesByType('navigation')[0]?.responseStatus", 'returnByValue': True})
                return value['result'].get('value') == 200
            wait_for(committed)
            tunnel_pid = store.runtime('dev').processes['tunnel']['pid']
            manager.browser_action('dev', 'stop')
            check('browser Stop keeps tunnel and reservation', manager.tunnels.observe('dev')['state'] == 'listening'
                  and store.runtime('dev').processes['tunnel']['pid'] == tunnel_pid and store.runtime('dev').reserved_port == port)
            manager.browser_action('dev', 'start')
            restored = wait_for(lambda: pages('dev') if len(pages('dev')) == 3 else None)
            check('session restored added tab without duplicate initial URLs', len(restored) == 3
                  and sum(urlsplit(entry['url']).fragment == 'devhard-session-probe' for entry in restored) == 1
                  and {urlsplit(entry['url']).netloc for entry in restored} == {urlsplit(url).netloc for url in args.http_url})
            saved = store.runtime('dev').processes['browser']
            invalid = replace(store.runtime('dev'), processes={**store.runtime('dev').processes, 'browser': {**saved, 'started': 'macos:0:0'}})
            store.save_runtime('dev', invalid)
            try:
                manager.browser_action('dev', 'stop')
                raise AssertionError('mismatching browser identity accepted')
            except TunnelError as error:
                check('stale PID identity refused without killing Chrome', error.code == 'browser_ownership_unknown' and process.identity(saved) == 'owned')
            manager.browsers.save('dev', record=saved, error=None)
        check('Manager normal exit preserves both processes', process.identity(store.runtime('dev').processes['browser']) == 'owned'
              and process.identity(store.runtime('dev').processes['tunnel']) == 'owned')
        cli = [sys.executable, '-m', 'devhard', '--data-dir', str(root)]
        queried = subprocess.run(cli + ['browser', 'status', 'dev'], capture_output=True, text=True, timeout=15)
        check('CLI browser status', queried.returncode == 0 and json.loads(queried.stdout)['state'] == 'running')
        saved_pid = store.runtime('dev').processes['browser']['pid']
        repeated = subprocess.run(cli + ['browser', 'start', 'dev'], capture_output=True, text=True, timeout=15)
        check('CLI browser Start is idempotent', repeated.returncode == 0 and store.runtime('dev').processes['browser']['pid'] == saved_pid)
        with Manager(store) as manager:
            manager.register(WorkspaceConfig('broken', args.ssh_host, executable='/nonexistent/devhard-chrome'))
        partial = subprocess.run(cli + ['workspace', 'start-all', 'broken'], capture_output=True, text=True, timeout=25)
        check('CLI partial Start All retains healthy tunnel', partial.returncode == 1
              and json.loads(partial.stdout)['result'] == 'partial'
              and store.runtime('broken').processes['tunnel'] is not None
              and store.runtime('broken').processes['browser'] is None)
        with (root / 'foreground.log').open('wb') as log:
            foreground = subprocess.Popen([sys.executable, '-m', 'devhard', '--data-dir', str(root), 'manager', 'run'], stdout=log, stderr=log)
        def ready():
            info = store.read(store.safe('runtime', 'manager.json'))
            return info['pid'] == foreground.pid and info['phase'] == 'ready'
        wait_for(ready)
        foreground.kill()
        foreground.wait(timeout=5)
        with Manager(store) as manager:
            enable(manager)
            check('SIGKILL Manager then rediscover browser and tunnel', manager.browsers.observe('dev')['state'] == 'running'
                  and manager.tunnels.observe('dev')['state'] == 'listening')
            # Simulate interrupted spawn before runtime process publication using real Chrome.
            manager.register(WorkspaceConfig('journal', args.ssh_host))
            state = manager.tunnels.reserve('journal')
            marker = f'--devhard-instance={uuid.uuid4().hex}'
            argv = manager.browsers.command(store.workspace('journal'), state, marker)
            atomic_write(manager.browsers.journal_path('journal'), json_text({
                'schema_version': 1, 'argv': argv, 'marker': marker, 'reserved_port': state.reserved_port}))
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            manual_children.append(child)
            wait_for(lambda: process.profile_processes(manager.browsers.profile('journal')) == [child.pid])
            manager.browsers.reconcile('journal')
            check('real Chrome journal recovers missing process record', store.runtime('journal').processes['browser']['pid'] == child.pid)
            child.kill()
            child.wait(timeout=5)
            check('unexpected Chrome exit is observed', manager.browsers.observe('journal')['state'] == 'exited')
            manager.browser_action('journal', 'start')
            check('Chrome restarts after unexpected exit with same reservation', manager.browsers.observe('journal')['state'] == 'running'
                  and store.runtime('journal').reserved_port == state.reserved_port)
            manager.workspace_action('journal', 'stop_all')
            child.wait(timeout=5)
            manager.register(WorkspaceConfig('foreign', args.ssh_host))
            profile = manager.browsers.profile('foreign')
            alias = root / 'foreign-profile-alias'
            alias.symlink_to(profile, target_is_directory=True)
            executable = manager.browsers.executable('auto')
            child = subprocess.Popen([executable, f'--user-data-dir={alias}', '--no-first-run', '--no-default-browser-check', 'about:blank'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            manual_children.append(child)
            wait_for(lambda: process.profile_processes(alias) == [child.pid] and (profile / 'SingletonLock').is_symlink())
            for action in ('start', 'stop'):
                try:
                    manager.browser_action('foreign', action)
                    raise AssertionError('unmanaged profile accepted')
                except TunnelError as error:
                    check(f'unmanaged profile {action} refused', error.code == 'browser_ownership_unknown' and child.poll() is None)
            child.terminate()
            child.wait(timeout=10)
            for name in ('dev', 'ops'):
                profile_marker = manager.browsers.profile(name) / 'probe-marker'
                profile_marker.write_text('keep')
                result = manager.workspace_action(name, 'stop_all')
                check(f'{name}: Stop All releases port and preserves profile', result['result'] == 'success'
                      and store.runtime(name).reserved_port is None and profile_marker.read_text() == 'keep')
        evidence['success'] = True
    except BaseException as error:
        evidence['success'] = False
        evidence['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if foreground is not None and foreground.poll() is None:
            foreground.terminate()
            foreground.wait(timeout=5)
        for child in manual_children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        errors = []
        try:
            with Manager(store) as manager:
                for config in store.workspaces():
                    try:
                        manager.workspace_action(config.name, 'stop_all')
                    except Exception as error:
                        errors.append(str(error))
        except Exception as error:
            errors.append(str(error))
        evidence['cleanup_errors'] = errors
        if errors:
            evidence['success'] = False
        atomic_write(root / 'result.json', json_text(evidence))
        print(f'Evidence: {root / "result.json"}', flush=True)
        if errors:
            raise RuntimeError(f'cleanup incomplete: {errors}')


if __name__ == '__main__':
    main()
