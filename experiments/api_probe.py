"""Exercise a real M4 HTTP server against disposable Docker SSH endpoints."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request

from browser_probe import OPENER, wait_for
from devhard import process
from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-state', type=Path, default=Path('.fixture-runs/active.json'))
    parser.add_argument('--chrome', action='store_true')
    args = parser.parse_args()
    fixture = json.loads(args.fixture_state.read_text())
    base = Path(__file__).resolve().parents[1] / '.m4-runs'
    base.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='probe-', dir=base))
    atomic_write(root / 'config.toml', 'schema_version = 1\n[ssh]\nconfig_file = '
                 + json.dumps(fixture['ssh_config']) + '\n')
    evidence = {'root': str(root), 'checks': [], 'cleanup_errors': []}
    server = None
    url, token = None, None

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        evidence['checks'].append(label)
        print('PASS ' + label, flush=True)

    def request(path, method='GET', body=None, headers=None):
        auth = {'Authorization': 'Bearer ' + token, 'Origin': url}
        if headers is not None:
            auth = headers
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            auth['Content-Type'] = 'application/json'
        try:
            with OPENER.open(Request(url + path, data=data, headers=auth, method=method), timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def start_server():
        nonlocal server, url, token
        with (root / 'server.log').open('ab') as log:
            server = subprocess.Popen([sys.executable, '-m', 'devhard', '--data-dir', str(root), 'manager', 'run'],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)

        def ready():
            if server.poll() is not None:
                raise RuntimeError((root / 'server.log').read_text())
            path = root / 'runtime/manager.json'
            if path.exists():
                info = json.loads(path.read_text())
                if info['pid'] == server.pid and info['phase'] == 'ready' and info['api_url']:
                    return info['api_url']

        url = wait_for(ready)
        with OPENER.open(url, timeout=5) as response:
            page = response.read().decode()
        token = re.search(r'name="devhard-token" content="([^"]+)"', page)[1]

    def job(path, body, method='POST'):
        status, value = request(path, method, body)
        check('command accepted as HTTP 202: ' + path, status == 202 and value['state'] == 'queued')
        return value

    def finished(value):
        def poll():
            status, current = request('/api/jobs/' + value['id'])
            assert status == 200
            return current if current['state'] not in ('queued', 'running') else None
        return wait_for(poll, timeout=40)

    def action(name, action):
        return finished(job('/api/workspaces/' + name + '/' + action, {}))

    def identities():
        result = []
        for item in fixture['servers']:
            state = Store(root).runtime(item['alias'])
            for port in (3000, 6767):
                response = subprocess.run(['curl', '--noproxy', '', '--proxy',
                    f'socks5h://127.0.0.1:{state.reserved_port}', '--fail', '--silent', '--show-error',
                    '--max-time', '10', f'http://127.0.0.1:{port}/identity'], capture_output=True, text=True, check=True)
                body = json.loads(response.stdout)
                check(f'{item["alias"]}:{port} correct server', body == {'fixture_id': item['fixture_id'], 'port': port})
                result.append(body)
        return result

    try:
        start_server()
        check('API requires token', request('/api/state', headers={})[0] == 401)
        check('foreign Origin rejected', request('/api/state', headers={
            'Authorization': 'Bearer ' + token, 'Origin': 'https://untrusted.test'})[0] == 403)
        check('DNS rebinding Host rejected', request('/api/state', headers={
            'Authorization': 'Bearer ' + token, 'Host': 'untrusted.test'})[0] == 403)
        for item in fixture['servers']:
            config = WorkspaceConfig(item['alias'], item['alias'],
                                     ('http://127.0.0.1:3000', 'http://127.0.0.1:6767'))
            check('registration completed', finished(job('/api/workspaces', config.as_dict()))['state'] == 'succeeded')
        component = 'workspace/start_all' if args.chrome else 'tunnel/start'
        with ThreadPoolExecutor(2) as pool:
            submitted = list(pool.map(lambda item: job('/api/workspaces/' + item['alias'] + '/' + component, {}), fixture['servers']))
        started = time.monotonic()
        check('poll responds during commands', request('/api/state')[0] == 200 and time.monotonic() - started < 2)
        check('two concurrent clients finish successfully', all(finished(j)['state'] == 'succeeded' for j in submitted))
        evidence['identities'] = identities()
        saved = {item['alias']: Store(root).runtime(item['alias']).as_dict() for item in fixture['servers']}
        for name, state in saved.items():
            check(name + ' native process ownership', all(process.identity(r) == 'owned' for r in state['processes'].values() if r))
        first = fixture['servers'][0]
        updated = WorkspaceConfig(first['alias'], 'changed-host').as_dict()
        check('editing active workspace refused', finished(job('/api/workspaces/' + first['alias'], updated, 'PUT'))['state'] == 'failed')
        old_token = token
        server.terminate()
        check('Manager normal shutdown exits zero', server.wait(timeout=25) == 0)
        check('normal shutdown preserves native children', all(process.identity(r) == 'owned'
              for state in saved.values() for r in state['processes'].values() if r))
        start_server()
        check('old token expires on restart', request('/api/state', headers={'Authorization': 'Bearer ' + old_token})[0] == 401)
        _, state = request('/api/state')
        check('new Manager rediscovers running processes', all(w['status']['tunnel'] == 'listening'
              and (not args.chrome or w['status']['browser'] == 'running') for w in state['workspaces']))
        check('job history resets on restart', state['jobs'] == [])
        check('process PIDs and reservations unchanged', all(Store(root).runtime(name).as_dict() == runtime for name, runtime in saved.items()))
        server.kill()
        server.wait(timeout=5)
        start_server()
        check('API rediscovers children after Manager SIGKILL', all(Store(root).runtime(name).as_dict() == runtime
              for name, runtime in saved.items()) and request('/api/state')[0] == 200)
        for item in fixture['servers']:
            check('Stop All succeeds', action(item['alias'], 'workspace/stop_all')['state'] == 'succeeded')
        _, state = request('/api/state')
        check('API reports stopped children and released reservations', all(
            w['runtime']['reserved_port'] is None and all(r is None for r in w['runtime']['processes'].values()) for w in state['workspaces']))
        partial = WorkspaceConfig('partial', first['alias'], executable='/nonexistent/devhard-chrome')
        finished(job('/api/workspaces', partial.as_dict()))
        result = action('partial', 'workspace/start_all')
        check('real partial success retains SSH and reports browser failure', result['state'] == 'partial'
              and result['result']['workspace']['status']['tunnel'] == 'listening')
        check('partial workspace cleanup', action('partial', 'workspace/stop_all')['state'] == 'succeeded')
        evidence['success'] = True
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait(timeout=25)
        try:
            with Manager(Store(root)) as manager:
                for config in manager.store.workspaces():
                    try:
                        manager.workspace_action(config.name, 'stop_all')
                    except Exception as error:
                        evidence['cleanup_errors'].append(str(error))
        finally:
            atomic_write(root / 'result.json', json_text(evidence))
            print('Evidence: ' + str(root / 'result.json'))
        if evidence['cleanup_errors']:
            raise RuntimeError(evidence['cleanup_errors'])


if __name__ == '__main__':
    main()
