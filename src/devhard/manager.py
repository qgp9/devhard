"""Exclusive Manager ownership and startup reconciliation, without process control."""

from datetime import datetime, timezone
import errno
import fcntl
import os
import uuid

from .config import DevHardError, InvalidConfig
from .store import Store, atomic_write, json_text


class AlreadyRunning(DevHardError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def open_lock(store):
    path = store.safe('runtime', 'manager.lock')
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


class Manager:
    def __init__(self, store: Store):
        self.store = store
        self.fd = None
        self.ready = False
        self.instance_id = uuid.uuid4().hex
        self.started_at = now()
        self.restored = []

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
            # Reconcile before publishing ready. M1 never assumes saved PIDs are owned.
            reservations = {}
            self.restored = []
            for config in self.store.workspaces():
                state = self.store.runtime(config.name)
                if state.reserved_port is not None:
                    if state.reserved_port in reservations:
                        raise InvalidConfig(f'duplicate reserved port {state.reserved_port}: '
                                            f'{reservations[state.reserved_port]} and {config.name}')
                    reservations[state.reserved_port] = config.name
                self.restored.append(self.view(config, state))
            self.ready = True
            self.publish('ready')
            return self
        except BaseException:
            self.ready = False
            self.store._write_enabled = False
            os.close(self.fd)
            self.fd = None
            raise

    def view(self, config, state):
        return {**config.as_dict(), 'profile_path': str(self.store.safe('workspaces', config.name, 'chrome-profile')),
                'runtime': state.as_dict(), 'status': {
                    key: 'unknown' if value is not None else 'stopped'
                    for key, value in state.processes.items()}}

    def publish(self, phase):
        self.info = {'schema_version': 1, 'pid': os.getpid(), 'started_at': self.started_at,
                     'instance_id': self.instance_id, 'data_root': str(self.store.root),
                     'phase': phase, 'api_url': None, 'restored_count': len(self.restored)}
        atomic_write(self.store.safe('runtime', 'manager.json'), json_text(self.info))

    def register(self, config):
        if not self.ready:
            raise DevHardError('Manager is not ready')
        result = self.store.register(config)
        self.restored.append(self.view(result, self.store.runtime(result.name)))
        self.publish('ready')
        return result

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
