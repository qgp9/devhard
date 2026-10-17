"""CLI. All mutation goes through the same Manager lock and core."""

import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from .config import DevHardError, WorkspaceConfig
from .manager import AlreadyRunning, Manager, status
from .store import Store
from .tunnel import TunnelController, TunnelError


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='devhard')
    parser.add_argument('--data-dir', type=Path, help='data root (default: ~/.devhard)')
    commands = parser.add_subparsers(dest='command', required=True)
    manager_parser = commands.add_parser('manager').add_subparsers(dest='action', required=True)
    manager_parser.add_parser('run', help='run the foreground Manager; Ctrl-C to exit')
    manager_parser.add_parser('status', help='read OS lock state and last run metadata')
    workspace_parser = commands.add_parser('workspace').add_subparsers(dest='action', required=True)
    add = workspace_parser.add_parser('add')
    add.add_argument('name')
    add.add_argument('--ssh-host', required=True)
    add.add_argument('--tab', action='append', default=[])
    workspace_parser.add_parser('list')
    show = workspace_parser.add_parser('show')
    show.add_argument('name')
    stop_all = workspace_parser.add_parser('stop-all', help='stop managed components and release the reservation')
    stop_all.add_argument('name')
    tunnel_parser = commands.add_parser('tunnel').add_subparsers(dest='action', required=True)
    for action in ('start', 'stop', 'restart', 'status'):
        tunnel_parser.add_parser(action).add_argument('name')
    args = parser.parse_args(argv)
    try:
        store = Store(args.data_dir)
        if args.command == 'tunnel':
            store.workspace(args.name)
            if args.action == 'status':
                emit(TunnelController(store).observe(args.name))
            else:
                with Manager(store) as manager:
                    emit(manager.tunnel_action(args.name, args.action))
        elif args.command == 'workspace':
            if args.action == 'add':
                config = WorkspaceConfig.parse({'schema_version': 1, 'name': args.name,
                                                'ssh': {'host': args.ssh_host}, 'tabs': args.tab})
                with Manager(store) as manager:
                    result = manager.register(config)
                    emit(result.as_dict())
            elif args.action == 'list':
                emit([config.as_dict() for config in store.workspaces()])
            elif args.action == 'stop-all':
                with Manager(store) as manager:
                    emit(manager.tunnel_action(args.name, 'stop_all'))
            else:
                config = store.workspace(args.name)
                emit({'config': config.as_dict(), 'runtime': store.runtime(args.name).as_dict()})
        elif args.action == 'status':
            emit(status(store))
        else:
            stop = threading.Event()
            previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                with Manager(store) as manager:
                    emit({**manager.info, 'workspaces': manager.restored})
                    stop.wait()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0
    except TunnelError as error:
        print(json.dumps({'code': error.code, 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except AlreadyRunning as error:
        print(f'devhard: {error}', file=sys.stderr)
        return 73
    except (DevHardError, OSError) as error:
        print(f'devhard: {error}', file=sys.stderr)
        return 1
