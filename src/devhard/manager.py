"""Exclusive Manager ownership, reconciliation, and serialized core operations."""

from datetime import datetime, timezone
import errno
import fcntl
import os
import subprocess
import threading
import uuid

from .config import DevHardError, InvalidConfig, WorkspaceConfig
from .store import Store, atomic_write, json_text, workspace_toml
from . import process
from .tunnel import TunnelController, TunnelError
from .browser import BrowserController


class AlreadyRunning(DevHardError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def open_lock(store):
    path = store.safe('runtime', 'manager.lock')
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


class Manager:
    def __init__(self, store: Store, *, initial_phase='ready'):
        self.store = store
        self.fd = None
        self.ready = False
        self.instance_id = uuid.uuid4().hex
        self.started_at = now()
        self.restored = []
        self.tunnels = TunnelController(store)
        self.browsers = BrowserController(store, self.tunnels)
        self.operations = threading.RLock()
        self.api_url = None
        self.initial_phase = initial_phase

    def __enter__(self):
        if self.fd is not None:
            raise DevHardError('Manager is already open')
        self.store.prepare_lock_directory()
        fd = open_lock(self.store)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            try:
                previous = self.store.read(self.store.safe('runtime', 'manager.json'))
                detail = f' (last recorded PID {previous.get("pid")}, instance {previous.get("instance_id")})'
            except (OSError, InvalidConfig, AttributeError):
                detail = ''
            raise AlreadyRunning(f'Manager already running for {self.store.root}{detail}') from None
        self.fd = fd
        self.store._write_enabled = True
        try:
            self.store.initialize()
            # Reconcile before publishing ready; uncertainty never authorizes control.
            reservations = {}
            self.restored = []
            for config in self.store.workspaces():
                state = self.store.runtime(config.name)
                if state.reserved_port is not None:
                    if state.reserved_port in reservations:
                        raise InvalidConfig(f'duplicate reserved port {state.reserved_port}: '
                                            f'{reservations[state.reserved_port]} and {config.name}')
                    reservations[state.reserved_port] = config.name
                try:
                    self.tunnels.reconcile(config.name)
                except (process.InspectionError, TunnelError, subprocess.TimeoutExpired):
                    pass  # Journal remains visible as unknown; control stays blocked.
                try:
                    self.browsers.reconcile(config.name)
                except (process.InspectionError, TunnelError, subprocess.TimeoutExpired):
                    pass
                self.restored.append(self.view(config, self.store.runtime(config.name)))
            self.ready = True
            self.publish(self.initial_phase)
            return self
        except BaseException:
            self.ready = False
            self.store._write_enabled = False
            os.close(self.fd)
            self.fd = None
            raise

    def view(self, config, state):
        tunnel = self.tunnels.observe(config.name)
        browser = self.browsers.observe(config.name)
        return {'name': config.name, 'config': config.as_dict(),
                'profile_path': str(self.store.safe('workspaces', config.name, 'chrome-profile')),
                'runtime': state.as_dict(), 'tunnel': tunnel, 'browser': browser, 'status': {
                    key: 'unknown' if value is not None else 'stopped'
                    for key, value in state.processes.items()} | {'tunnel': tunnel['state'], 'browser': browser['state']}}

    def publish(self, phase):
        self.info = {'schema_version': 1, 'pid': os.getpid(), 'started_at': self.started_at,
                     'instance_id': self.instance_id, 'data_root': str(self.store.root),
                     'phase': phase, 'api_url': self.api_url, 'restored_count': len(self.restored)}
        atomic_write(self.store.safe('runtime', 'manager.json'), json_text(self.info))

    def register(self, config):
        with self.operations:
            if not self.ready:
                raise DevHardError('Manager is not ready')
            result = self.store.register(config)
            self.restored.append(self.view(result, self.store.runtime(result.name)))
            self.publish('ready')
            return result

    def tunnel_action(self, name, action):
        with self.operations:
            if not self.ready:
                raise DevHardError('Manager is not ready')
            if action == 'stop_all':
                return self.workspace_action(name, 'stop_all')
            self.store.workspace(name)
            if action not in ('start', 'stop', 'restart', 'stop_all'):
                raise DevHardError(f'unsupported tunnel action: {action}')
            result = getattr(self.tunnels, action)(name)
            self.restored = [self.view(config, self.store.runtime(config.name)) for config in self.store.workspaces()]
            return result

    def update(self, config):
        """Edit a stopped workspace without replacing its profile or runtime."""
        with self.operations:
            if not self.ready:
                raise DevHardError('Manager is not ready')
            config = WorkspaceConfig.parse(config.as_dict())
            self.store.workspace(config.name)
            for controller in (self.tunnels, self.browsers):
                if controller.observe(config.name)['state'] not in ('stopped', 'exited', 'error'):
                    raise DevHardError('Stop both Chrome and SSH before editing this workspace')
            atomic_write(self.store.safe('workspaces', config.name, 'config.toml'), workspace_toml(config))
            return config

    def browser_action(self, name, action):
        with self.operations:
            if not self.ready:
                raise DevHardError('Manager is not ready')
            self.store.workspace(name)
            if action not in ('start', 'stop', 'restart'):
                raise DevHardError(f'unsupported browser action: {action}')
            return getattr(self.browsers, action)(name)

    def workspace_action(self, name, action):
        with self.operations:
            if not self.ready:
                raise DevHardError('Manager is not ready')
            config = self.store.workspace(name)
            if action == 'start_all':
                self.tunnels.start(name)
                try:
                    self.browsers.start(name)
                except (DevHardError, OSError) as error:
                    return {'result': 'partial', 'error': {'code': getattr(error, 'code', 'browser_start_failed'), 'message': str(error)},
                            'workspace': self.view(config, self.store.runtime(name))}
            elif action == 'stop_all':
                self.browsers.stop(name)
                self.tunnels.stop_all(name)
            else:
                raise DevHardError(f'unsupported workspace action: {action}')
            return {'result': 'success', 'workspace': self.view(config, self.store.runtime(name))}

    def __exit__(self, *_):
        if self.fd is not None:
            self.ready = False
            try:
                self.publish('stopped')
            finally:
                self.store._write_enabled = False
                os.close(self.fd)
                self.fd = None


def status(store):
    """Best-effort snapshot: OS lock is authoritative; metadata may be stale."""
    path = store.safe('runtime', 'manager.lock')
    active = False
    if path.exists():
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                active = True
        finally:
            os.close(fd)
    metadata = store.safe('runtime', 'manager.json')
    try:
        info = store.read(metadata) if metadata.exists() else None
    except InvalidConfig:
        info = {'error': 'invalid manager metadata; OS lock status remains authoritative'}
    return {'active': active, 'data_root': str(store.root), 'last_run': info}
