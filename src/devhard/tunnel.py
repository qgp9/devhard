"""Reserved loopback ports and detached OpenSSH tunnels. Calls require the Manager lock."""

from dataclasses import replace
from datetime import datetime, timezone
import os
import errno
from pathlib import Path
import random
import shutil
import signal
import socket
import subprocess
import time
import uuid

from . import process
from .config import DevHardError, InvalidConfig, fields, integer, version
from .store import atomic_write, json_text


class TunnelError(DevHardError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def available(port):
    try:
        with socket.socket() as sock:
            # Match OpenSSH listener reuse semantics; TIME_WAIT is not an active listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
        return True
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return False
        raise


def socks_ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.3) as stream:
            stream.sendall(b'\x05\x01\x00')
            reply = b''
            while len(reply) < 2:
                data = stream.recv(2 - len(reply))
                if not data:
                    return False
                reply += data
            return reply == b'\x05\x00'
    except OSError:
        return False


def classify(detail):
    lower = detail.lower()
    if 'remote host identification has changed' in lower:
        return 'host_key_changed'
    if 'host key verification failed' in lower:
        return 'host_key_verification'
    if 'permission denied' in lower or 'authentication failed' in lower:
        return 'authentication_failed'
    if 'address already in use' in lower or 'cannot listen to port' in lower:
        return 'port_conflict'
    if 'could not resolve hostname' in lower:
        return 'dns_failed'
    if any(value in lower for value in ('connection refused', 'no route to host', 'network is unreachable')):
        return 'connection_failed'
    if 'timed out' in lower:
        return 'connection_timeout'
    return 'ssh_exited'


class TunnelController:
    def __init__(self, store, *, start_timeout=15, stop_timeout=5):
        self.store = store
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.children = {}  # Reap children launched by this Manager, never kill on Manager exit.

    def journal_path(self, name):
        return self.store.safe('workspaces', name, 'runtime', 'tunnel-launch.json')

    def save(self, name, state, *, record=..., port=..., error=...):
        processes = dict(state.processes)
        if record is not ...:
            processes['tunnel'] = record
        updated = replace(state, processes=processes,
                          reserved_port=state.reserved_port if port is ... else port,
                          last_error=state.last_error if error is ... else error)
        self.store.save_runtime(name, updated)
        return updated

    def fail(self, name, code, message):
        state = self.store.runtime(name)
        error = {'code': code, 'message': message, 'at': datetime.now(timezone.utc).isoformat()}
        self.save(name, state, error=error)
        raise TunnelError(code, message)

    def reconcile(self, name):
        """Recover a write-ahead launch after Manager death; uncertainty blocks control."""
        self.store.require_writer()
        path = self.journal_path(name)
        if not path.exists():
            return
        journal = self.store.read(path)
        fields(journal, ['schema_version', 'marker', 'argv', 'reserved_port'],
               ['schema_version', 'marker', 'argv', 'reserved_port'])
        version(journal['schema_version'])
        integer(journal['reserved_port'], 1, 65535, 'launch reserved_port')
        argv, marker = journal['argv'], journal['marker']
        if (not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv)
                or not isinstance(marker, str) or not marker.startswith('DEVHARD_INSTANCE=')
                or f'SetEnv={marker}' not in argv):
            raise InvalidConfig('invalid SSH launch journal')
        state = self.store.runtime(name)
        if journal['reserved_port'] != state.reserved_port:
            raise InvalidConfig('launch journal port does not match reservation')
        existing = state.processes['tunnel']
        if existing is not None and existing['marker'] != marker:
            raise InvalidConfig('launch journal conflicts with saved tunnel')
        found = process.find_launch(argv, marker)
        if len(found) != 1:
            raise TunnelError('launch_uncertain', 'Interrupted SSH launch: cannot establish exactly one owned process; '
                              'inspect tunnel-launch.json and processes before clearing the journal')
        self.save(name, state, record=found[0])
        path.unlink()

    def observe(self, name):
        state = self.store.runtime(name)
        result = {'state': 'stopped', 'port': state.reserved_port, 'process_alive': False,
                  'listener_owned': False, 'socks_responding': False,
                  'identity_verified': False, 'last_error': state.last_error}
        record = state.processes['tunnel']
        child = self.children.get(name)
        if child is not None:
            child.poll()
        if self.journal_path(name).exists():
            return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'interrupted launch journal'}
        if record is None:
            if state.last_error:
                result['state'] = 'error'
            return result
        try:
            ownership = process.identity(record)
            if ownership == 'unknown':
                return {**result, 'state': 'unknown', 'process_alive': None,
                        'reason': 'process ownership could not be verified'}
            if ownership == 'exited':
                return {**result, 'state': 'exited'}
            result['process_alive'] = True
            result['listener_owned'] = process.owns_listener(record['pid'], state.reserved_port)
            result['socks_responding'] = result['listener_owned'] and socks_ready(state.reserved_port)
            result['state'] = 'listening' if result['socks_responding'] else 'running'
            return result
        except (process.InspectionError, subprocess.TimeoutExpired) as error:
            return {**result, 'state': 'unknown', 'reason': str(error)}

    def reserve(self, name, excluded=()):
        self.store.require_writer()
        state = self.store.runtime(name)
        if state.reserved_port is not None:
            return state
        config = self.store.global_config()
        used = {self.store.runtime(ws.name).reserved_port for ws in self.store.workspaces()}
        candidates = [port for port in range(config.port_min, config.port_max + 1)
                      if port not in used and port not in excluded]
        random.SystemRandom().shuffle(candidates)
        for port in candidates:
            if available(port):
                return self.save(name, state, port=port)
        self.fail(name, 'port_range_exhausted', 'No available unreserved port in the configured range')

    def ssh_command(self, host, port, marker):
        executable = shutil.which('ssh')
        if not executable:
            raise TunnelError('ssh_missing', 'OpenSSH executable was not found')
        argv = [str(Path(executable).resolve()), '-N', '-T', '-S', 'none']
        for option in ('BatchMode=yes', 'ExitOnForwardFailure=yes', 'ForkAfterAuthentication=no',
                       'ControlMaster=no', 'PermitLocalCommand=no', 'RemoteCommand=none',
                       'ForwardAgent=no', 'ForwardX11=no', 'ConnectTimeout=10', 'ConnectionAttempts=1',
                       'ServerAliveInterval=15', 'ServerAliveCountMax=3', f'SetEnv={marker}'):
            argv += ['-o', option]
        # SetEnv is an inert ownership marker here: -N opens no remote command session.
        return argv + ['-D', f'127.0.0.1:{port}', host]

    def start(self, name):
        self.store.require_writer()
        config = self.store.workspace(name)
        try:
            self.reconcile(name)
        except (process.InspectionError, TunnelError, subprocess.TimeoutExpired) as error:
            self.fail(name, getattr(error, 'code', 'inspection_failed'), str(error))
        state = self.store.runtime(name)
        observed = self.observe(name)
        if observed['state'] == 'listening':
            return observed
        if observed['state'] in ('unknown', 'running'):
            self.fail(name, 'ownership_unknown' if observed['state'] == 'unknown' else 'tunnel_not_ready',
                      'Existing tunnel cannot be started again: ' + observed.get('reason', observed['state']))
        if state.processes['tunnel'] is not None:
            state = self.save(name, state, record=None)
        new_reservation = state.reserved_port is None
        attempted = set()
        for attempt in range(3):
            state = self.reserve(name, excluded=attempted)
            port = state.reserved_port
            attempted.add(port)
            if not available(port):
                if new_reservation and attempt < 2:
                    self.save(name, state, port=None)
                    continue
                self.fail(name, 'port_conflict', f'Reserved port {port} is occupied; reservation was retained')
            marker = f'DEVHARD_INSTANCE={uuid.uuid4().hex}'
            try:
                argv = self.ssh_command(config.ssh_host, port, marker)
                self.launch(name, state, argv, marker)
                return self.observe(name)
            except TunnelError as error:
                if error.code == 'port_conflict' and new_reservation and attempt < 2:
                    self.save(name, self.store.runtime(name), port=None)
                    continue
                self.fail(name, error.code, str(error))
        self.fail(name, 'port_range_exhausted', 'Port allocation retries exhausted')

    def launch(self, name, state, argv, marker):
        journal = self.journal_path(name)
        atomic_write(journal, json_text({'schema_version': 1, 'argv': argv, 'marker': marker,
                                        'reserved_port': state.reserved_port}))
        log_path = self.store.safe('workspaces', name, 'runtime', 'tunnel.log')
        child = None
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab', buffering=0) as log:
                offset = os.fstat(log.fileno()).st_size
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, close_fds=True,
                                         env={**os.environ, 'LC_ALL': 'C'})
            self.children[name] = child
            deadline = time.monotonic() + self.start_timeout
            record = None

            def exited_error():
                with log_path.open('rb') as log:
                    log.seek(offset)
                    detail = log.read()[-2000:].decode(errors='replace').strip()
                return TunnelError(classify(detail), detail or f'SSH exited ({child.returncode})')

            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise exited_error()
                if record is None:
                    try:
                        record = process.capture(child.pid, argv, marker)
                    except process.InspectionError as inspection:
                        try:
                            child.wait(timeout=.05)
                        except subprocess.TimeoutExpired:
                            raise inspection
                        raise exited_error()
                    state = self.save(name, state, record=record, error=None)
                if process.owns_listener(child.pid, state.reserved_port) and socks_ready(state.reserved_port):
                    # A successful SOCKS response at a foreign listener is insufficient.
                    if child.poll() is None and process.identity(record) == 'owned':
                        journal.unlink()
                        return
                time.sleep(.1)
            raise TunnelError('start_timeout', 'SSH did not own a responding SOCKS listener before the start deadline')
        except BaseException as error:
            # Popen still owns an unreaped child here: no saved-PID inference is used.
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    raise TunnelError('stop_timeout', 'Failed launch is still running; journal retained') from error
            self.save(name, self.store.runtime(name), record=None)
            journal.unlink(missing_ok=True)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, TunnelError):
                raise
            if isinstance(error, process.InspectionError):
                raise TunnelError('inspection_failed', str(error)) from error
            if isinstance(error, (OSError, subprocess.TimeoutExpired)):
                raise TunnelError('launch_failed', str(error)) from error
            raise

    def stop(self, name):
        self.store.require_writer()
        try:
            self.reconcile(name)
            state = self.store.runtime(name)
            record = state.processes['tunnel']
            if record is None:
                self.save(name, state, error=None)
                return self.observe(name)
            ownership = process.identity(record)
            if ownership == 'unknown':
                self.fail(name, 'ownership_unknown', 'Refusing to stop a process whose identity does not match')
            if ownership == 'owned':
                # Recheck immediately before signalling; never signal a process group.
                if process.identity(record) != 'owned':
                    self.fail(name, 'ownership_unknown', 'Process identity changed before stop')
                try:
                    os.kill(record['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + self.stop_timeout
                while time.monotonic() < deadline:
                    child = self.children.get(name)
                    if child is not None:
                        child.poll()
                    try:
                        ownership = process.identity(record)
                    except (process.InspectionError, subprocess.TimeoutExpired):
                        ownership = 'unknown'
                    if ownership == 'exited':
                        break
                    # ps can observe a transient exit-time command change. Do not
                    # signal again or clear the record until absence is verified.
                    time.sleep(.05)
                else:
                    self.fail(name, 'ownership_unknown' if ownership == 'unknown' else 'stop_timeout',
                              'SSH exit could not be confirmed before the stop deadline; reservation retained')
            self.save(name, state, record=None, error=None)
            return self.observe(name)
        except (process.InspectionError, subprocess.TimeoutExpired) as error:
            self.fail(name, 'inspection_failed', str(error))

    def restart(self, name):
        self.stop(name)
        return self.start(name)

    def stop_all(self, name):
        self.store.require_writer()
        state = self.store.runtime(name)
        if state.processes['browser'] is not None:
            self.fail(name, 'browser_not_stopped', 'Browser record exists; M2 cannot verify or stop it. Reservation retained')
        self.stop(name)
        state = self.store.runtime(name)
        self.save(name, state, port=None, error=None)
        return self.observe(name)
