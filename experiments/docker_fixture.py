"""Build and manage two disposable SSH + loopback HTTP test endpoints."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / '.fixture-runs'
ACTIVE = BASE / 'active.json'
LABEL = 'devhard.fixture.run'
IMAGE = 'devhard-ssh-web:local'


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def docker(state, *args, capture=True):
    return run(['docker', '--context', state['context'], *args],
               capture_output=capture).stdout


def save(state):
    atomic_write(ACTIVE, json.dumps(state, indent=2) + '\n')


def inspect(state, cid):
    return json.loads(docker(state, 'inspect', cid))[0]


def owned(state, info):
    if info['Config'].get('Labels', {}).get(LABEL) != state['run_id']:
        raise RuntimeError('Refusing to operate on a container without this run label')


def down(state):
    for item in list(state['servers']):
        # An already removed container should not make the remaining cleanup impossible.
        existing = docker(state, 'ps', '-aq', '--no-trunc', '--filter', f'id={item["id"]}').split()
        if item['id'] in existing:
            info = inspect(state, item['id'])
            owned(state, info)
            docker(state, 'rm', '-f', item['id'])
        state['servers'].remove(item)
        save(state)
    ACTIVE.unlink()


def key(path):
    run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)])


def up(context):
    BASE.mkdir(mode=0o700, exist_ok=True)
    if ACTIVE.exists():
        raise RuntimeError('Fixture already recorded; use check or down first')
    context = context or run(['docker', 'context', 'show'], capture_output=True).stdout.strip()
    docker({'context': context}, 'build', '-t', IMAGE,
           str(REPO / 'tests/fixtures/ssh-web'), capture=False)
    run_id = uuid.uuid4().hex[:12]
    root = BASE / run_id
    root.mkdir(mode=0o700)
    key(root / 'client')
    state = {'context': context, 'image': IMAGE, 'run_id': run_id,
             'root': str(root), 'ssh_config': str(root / 'ssh_config'), 'servers': []}
    save(state)
    try:
        config, known = [], []
        for suffix in ('a', 'b'):
            keys = root / suffix
            keys.mkdir(mode=0o700)
            key(keys / 'ssh_host_ed25519_key')
            (keys / 'authorized_keys').write_bytes((root / 'client.pub').read_bytes())
            alias = f'devhard-fixture-{suffix}'
            cid = docker(state, 'run', '-d', '--name', f'devhard-fixture-{run_id}-{suffix}',
                         '--label', f'{LABEL}={run_id}', '--publish', '127.0.0.1::22',
                         '--mount', f'type=bind,src={keys},dst=/run/devhard-keys,readonly',
                         '--env', f'FIXTURE_ID=fixture-{suffix}', IMAGE).strip()
            item = {'id': cid, 'alias': alias, 'fixture_id': f'fixture-{suffix}'}
            state['servers'].append(item)
            save(state)
            deadline = time.monotonic() + 60
            while True:
                info = inspect(state, cid)
                if info['State'].get('Health', {}).get('Status') == 'healthy':
                    break
                if not info['State']['Running'] or time.monotonic() >= deadline:
                    raise RuntimeError(docker(state, 'logs', cid))
                time.sleep(.5)
            port = int(info['NetworkSettings']['Ports']['22/tcp'][0]['HostPort'])
            item['port'] = port
            pub = (keys / 'ssh_host_ed25519_key.pub').read_text().split()
            known.append(f'[127.0.0.1]:{port} {pub[0]} {pub[1]}\n')
            config.append(f'Host {alias}\n  HostName 127.0.0.1\n  Port {port}\n'
                          f'  User devhard\n  IdentityFile "{root / "client"}"\n'
                          f'  UserKnownHostsFile "{root / "known_hosts"}"\n'
                          '  IdentitiesOnly yes\n  StrictHostKeyChecking yes\n'
                          '  BatchMode yes\n  ConnectTimeout 5\n')
        atomic_write(root / 'known_hosts', ''.join(known))
        atomic_write(root / 'ssh_config', '\n'.join(config))
        save(state)
        return state
    except BaseException:
        down(state)
        raise


def check(state):
    root = Path(state['root']) / f'check-{uuid.uuid4().hex[:8]}'
    root.mkdir(mode=0o700)
    atomic_write(root / 'config.toml', 'schema_version = 1\n[ssh]\nconfig_file = '
                 + json.dumps(state['ssh_config']) + '\n')
    results = []
    with Manager(Store(root)) as manager:
        try:
            for item in state['servers']:
                info = inspect(state, item['id'])
                owned(state, info)
                assert info['State']['Health']['Status'] == 'healthy'
                assert info['NetworkSettings']['Ports'] == {
                    '22/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(item['port'])}]}
                # HTTP must not listen on the container network interface either.
                network = next(iter(info['NetworkSettings']['Networks'].values()))
                script = ('import socket; '
                          f'ip={network["IPAddress"]!r}; '
                          'assert all(socket.socket().connect_ex((ip,p)) != 0 '
                          'for p in (3000,6767))')
                docker(state, 'exec', item['id'], 'python', '-c', script)
                name = item['alias']
                manager.register(WorkspaceConfig(name, name))
                status = manager.tunnel_action(name, 'start')
                assert status['state'] == 'listening' and status['listener_owned']
                for port in (3000, 6767):
                    response = run(['curl', '--fail', '--silent', '--show-error',
                                    '--max-time', '10', '--noproxy', '', '--proxy',
                                    f'socks5h://127.0.0.1:{status["port"]}',
                                    f'http://127.0.0.1:{port}/identity'], capture_output=True)
                    body = json.loads(response.stdout)
                    assert body == {'fixture_id': item['fixture_id'], 'port': port}, body
                    results.append(body)
        finally:
            for config in manager.store.workspaces():
                manager.workspace_action(config.name, 'stop_all')
    atomic_write(root / 'result.json', json.dumps(results, indent=2) + '\n')
    print(json.dumps({'checked': results, 'evidence': str(root)}, indent=2))


def probe(state, chrome):
    common = ['--ssh-config', state['ssh_config'], '--ssh-host', state['servers'][0]['alias'],
              '--http-url', 'http://127.0.0.1:3000', '--http-url', 'http://127.0.0.1:6767']
    run([sys.executable, str(REPO / 'experiments/tunnel_probe.py'), *common])
    if chrome:
        run([sys.executable, str(REPO / 'experiments/browser_probe.py'), *common,
             '--second-ssh-host', state['servers'][1]['alias'],
             '--expected-first-id', state['servers'][0]['fixture_id'],
             '--expected-second-id', state['servers'][1]['fixture_id']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('up', 'check', 'probe', 'down', 'status'))
    parser.add_argument('--context', help='Docker context for up (default: current context)')
    parser.add_argument('--chrome', action='store_true', help='also run native Chrome probe')
    args = parser.parse_args()
    if args.action == 'up':
        state = up(args.context)
    else:
        state = json.loads(ACTIVE.read_text())
    if args.action == 'check':
        check(state)
    elif args.action == 'probe':
        probe(state, args.chrome)
    elif args.action == 'down':
        down(state)
    else:
        print(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()
