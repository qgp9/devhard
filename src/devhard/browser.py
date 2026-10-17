"""Independent native Chrome/Chromium instances with conservative profile ownership."""

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from . import process
from .config import InvalidConfig, fields, integer, version
from .store import atomic_write, json_text
from .tunnel import TunnelError, available


class BrowserError(TunnelError):
    pass


class BrowserController:
    def __init__(self, store, tunnels, *, start_timeout=10, stop_timeout=10, extra_args=()):
        self.store, self.tunnels = store, tunnels
        self.start_timeout, self.stop_timeout = start_timeout, stop_timeout
        self.extra_args = tuple(extra_args)  # Verification harness only; production uses no CDP flags.
        self.children = {}

    def profile(self, name):
        return self.store.safe('workspaces', name, 'chrome-profile')

    def journal_path(self, name):
        return self.store.safe('workspaces', name, 'runtime', 'browser-launch.json')

    def save(self, name, *, record=..., error=..., initialized=...):
        state = self.store.runtime(name)
        processes = dict(state.processes)
        if record is not ...:
            processes['browser'] = record
        updated = replace(state, processes=processes,
                          last_error=state.last_error if error is ... else error,
                          browser_initialized=state.browser_initialized if initialized is ... else initialized)
        self.store.save_runtime(name, updated)
        return updated

    def fail(self, name, code, message):
        self.save(name, error={'code': code, 'message': message, 'at': datetime.now(timezone.utc).isoformat()})
        raise BrowserError(code, message)

    def expected(self, name, record):
        state = self.store.runtime(name)
        return (record['marker'].startswith('--devhard-instance=')
                and f'--user-data-dir={self.profile(name)}' in record['argv']
                and f'--proxy-server=socks5://127.0.0.1:{state.reserved_port}' in record['argv'])

    def profile_locked(self, name):
        # Covers external Chrome launched through a relative/symlink profile alias,
        # which need not have our canonical path in its command line.
        lock = self.profile(name) / 'SingletonLock'
        if not lock.is_symlink():
            return lock.exists()
        target = os.readlink(lock)
        host, separator, pid = target.rpartition('-')
        if not separator or host != socket.gethostname() or not pid.isdecimal():
            return True
        numeric_pid = int(pid)
        return not 1 <= numeric_pid <= 2**31 - 1 or process.snapshot(numeric_pid) is not None

    def reconcile(self, name):
        self.store.require_writer()
        path = self.journal_path(name)
        if not path.exists():
            return
        raw = self.store.read(path)
        fields(raw, ['schema_version', 'argv', 'marker', 'reserved_port'],
               ['schema_version', 'argv', 'marker', 'reserved_port'])
        version(raw['schema_version'])
        integer(raw['reserved_port'], 1, 65535, 'browser launch port')
        argv, marker = raw['argv'], raw['marker']
        if (not isinstance(argv, list) or not all(isinstance(v, str) for v in argv)
                or not isinstance(marker, str) or not marker.startswith('--devhard-instance=')
                or marker not in argv or not self.expected(name, {'marker': marker, 'argv': argv})):
            raise InvalidConfig('invalid browser launch journal')
        state = self.store.runtime(name)
        if raw['reserved_port'] != state.reserved_port:
            raise InvalidConfig('browser launch port does not match reservation')
        existing = state.processes['browser']
        if existing is not None and existing['marker'] != marker:
            raise InvalidConfig('browser launch journal conflicts with runtime')
        found = process.find_launch(argv, marker)
        if len(found) != 1 or process.profile_processes(self.profile(name)) != [found[0]['pid']]:
            raise BrowserError('browser_launch_uncertain', 'Interrupted browser launch cannot be uniquely verified; journal retained')
        self.save(name, record=found[0], initialized=True)
        path.unlink()

    def observe(self, name):
        state = self.store.runtime(name)
        result = {'state': 'stopped', 'process_alive': False, 'port': state.reserved_port,
                  'profile_path': str(self.profile(name)), 'last_error': state.last_error}
        child = self.children.get(name)
        if child is not None:
            child.poll()
        if self.journal_path(name).exists():
            return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'interrupted browser launch journal'}
        record = state.processes['browser']
        try:
            if record is not None:
                if not self.expected(name, record):
                    return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'profile/proxy record mismatch'}
                identity = process.identity(record)
                if identity == 'unknown':
                    return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'browser ownership mismatch'}
                if identity == 'owned':
                    owners = process.profile_processes(self.profile(name))
                    if owners != [record['pid']]:
                        return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'profile has conflicting processes'}
                    return {**result, 'state': 'running', 'process_alive': True}
            if process.profile_processes(self.profile(name)):
                return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'unmanaged process uses this profile'}
            if self.profile_locked(name):
                return {**result, 'state': 'unknown', 'process_alive': None, 'reason': 'profile lock has a live or unverified owner'}
            return {**result, 'state': 'exited' if record is not None else 'stopped'}
        except (process.InspectionError, subprocess.TimeoutExpired) as error:
            return {**result, 'state': 'unknown', 'process_alive': None, 'reason': str(error)}

    def executable(self, configured):
        if configured != 'auto':
            path = Path(configured).expanduser()
            if not path.is_absolute():
                raise BrowserError('browser_missing', 'browser.executable must be auto or an absolute native executable path')
            candidates = [path]
        elif sys.platform == 'darwin':
            candidates = [Path(base) / f'{app}.app/Contents/MacOS/{app}'
                          for base in ('/Applications', str(Path.home() / 'Applications'))
                          for app in ('Google Chrome', 'Chromium')]
        else:
            candidates = [Path(path) for name in ('google-chrome', 'chromium', 'chromium-browser')
                          if (path := shutil.which(name))]
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
        raise BrowserError('browser_missing', 'Chrome/Chromium native executable not found')

    def command(self, config, state, marker):
        argv = [self.executable(config.executable), f'--user-data-dir={self.profile(config.name)}',
                f'--proxy-server=socks5://127.0.0.1:{state.reserved_port}', '--proxy-bypass-list=<-loopback>',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode', marker, *self.extra_args]
        if state.browser_initialized and config.restore_session:
            return argv + ['--restore-last-session']
        return argv + list(config.tabs or ('about:blank',))

    def start(self, name):
        self.store.require_writer()
        config = self.store.workspace(name)
        try:
            self.reconcile(name)
        except (BrowserError, process.InspectionError, subprocess.TimeoutExpired) as error:
            self.fail(name, getattr(error, 'code', 'inspection_failed'), str(error))
        observed = self.observe(name)
        if observed['state'] == 'running':
            return observed
        if observed['state'] == 'unknown':
            self.fail(name, 'browser_ownership_unknown', observed['reason'])
        self.save(name, record=None)
        state = self.tunnels.reserve(name)
        tunnel = self.tunnels.observe(name)
        if tunnel['state'] == 'unknown':
            self.fail(name, 'ownership_unknown', 'Tunnel ownership is unknown; refusing to connect browser')
        if tunnel['state'] != 'listening' and not available(state.reserved_port):
            self.fail(name, 'port_conflict', 'Browser proxy port is occupied by an unverified listener')
        marker = f'--devhard-instance={uuid.uuid4().hex}'
        try:
            argv = self.command(config, state, marker)
            self.launch(name, argv, marker, state.reserved_port)
            return self.observe(name)
        except BrowserError as error:
            self.fail(name, error.code, str(error))

    def launch(self, name, argv, marker, port):
        journal = self.journal_path(name)
        atomic_write(journal, json_text({'schema_version': 1, 'argv': argv, 'marker': marker, 'reserved_port': port}))
        child, record = None, None
        try:
            log_path = self.store.safe('workspaces', name, 'runtime', 'browser.log')
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab', buffering=0) as log:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, close_fds=True)
            self.children[name] = child
            started = time.monotonic()
            while time.monotonic() - started < self.start_timeout:
                if child.poll() is not None:
                    # The launcher PID may exit after handing off. Adopt only our unique launch.
                    found = process.find_launch(argv, marker)
                    if len(found) != 1:
                        raise BrowserError('browser_start_failed', f'Browser launcher exited ({child.returncode}); see browser.log')
                    record = found[0]
                elif record is None:
                    record = process.capture(child.pid, argv, marker)
                if (time.monotonic() - started >= .7 and process.identity(record) == 'owned'
                        and process.profile_processes(self.profile(name)) == [record['pid']]):
                    self.save(name, record=record, error=None, initialized=True)
                    journal.unlink()
                    return
                time.sleep(.1)
            raise BrowserError('browser_start_timeout', 'Browser did not establish a unique stable profile process')
        except BaseException as error:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    raise BrowserError('browser_stop_timeout', 'Failed launch is still running; journal retained') from error
            if child is not None:
                try:
                    remaining = process.find_launch(argv, marker)
                    if len(remaining) > 1:
                        raise BrowserError('browser_launch_uncertain', 'Multiple launch processes remain; journal retained')
                    for candidate in remaining:
                        self.terminate(candidate)
                except (process.InspectionError, subprocess.TimeoutExpired) as inspection:
                    raise BrowserError('browser_launch_uncertain', 'Cannot exclude a handed-off browser; journal retained') from inspection
            self.save(name, record=None)
            journal.unlink(missing_ok=True)
            if isinstance(error, (KeyboardInterrupt, SystemExit, BrowserError)):
                raise
            if isinstance(error, (process.InspectionError, subprocess.TimeoutExpired, OSError)):
                raise BrowserError('browser_start_failed', str(error)) from error
            raise

    def terminate(self, record):
        ownership = process.identity(record)
        if ownership == 'exited':
            return
        if ownership != 'owned' or process.identity(record) != 'owned':
            raise BrowserError('browser_ownership_unknown', 'Refusing to signal an unverified browser process')
        try:
            if not process.terminate_application(record['pid']):
                os.kill(record['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            for child in self.children.values():
                child.poll()
            try:
                if process.identity(record) == 'exited':
                    return
            except (process.InspectionError, subprocess.TimeoutExpired):
                pass  # No further signal; wait for verified absence.
            time.sleep(.05)
        raise BrowserError('browser_stop_timeout', 'Browser exit unconfirmed; record and reservation retained')

    def stop(self, name):
        self.store.require_writer()
        try:
            self.reconcile(name)
            observed = self.observe(name)
            if observed['state'] == 'unknown':
                self.fail(name, 'browser_ownership_unknown', observed['reason'])
            record = self.store.runtime(name).processes['browser']
            if record is not None:
                self.terminate(record)
            if process.profile_processes(self.profile(name)):
                self.fail(name, 'browser_ownership_unknown', 'Profile process remains after stop')
            self.save(name, record=None, error=None)
            return self.observe(name)
        except (BrowserError, process.InspectionError, subprocess.TimeoutExpired) as error:
            self.fail(name, getattr(error, 'code', 'inspection_failed'), str(error))

    def restart(self, name):
        self.stop(name)
        return self.start(name)
