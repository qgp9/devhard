"""Independent native Chrome/Chromium instances with conservative profile ownership."""

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

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
        if isinstance(raw, dict) and raw.get('purpose') == 'open_bookmarks':
            return self.reconcile_open(name, raw)
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
        return argv + list(config.tabs if not state.browser_initialized and config.tabs else ('about:blank',))

    def reconcile_open(self, name, raw):
        """A notifier must exit; never adopt a new browser created during an exit race."""
        fields(raw, ['purpose', 'argv', 'marker', 'schema_version', 'focus_result'],
               ['purpose', 'argv', 'marker', 'schema_version'])
        version(raw['schema_version'])
        argv, marker = raw['argv'], raw['marker']
        if (not isinstance(argv, list) or not all(isinstance(a, str) for a in argv)
                or not isinstance(marker, str) or marker not in argv or '--new-tab' not in argv
                or not self.expected(name, {'marker': marker, 'argv': argv})):
            raise InvalidConfig('invalid bookmark notification journal')
        result_path = None
        if 'focus_result' in raw:
            filename = raw['focus_result']
            suffix = marker.removeprefix('--devhard-instance=')
            if (not re.fullmatch(r'[0-9a-f]{32}', suffix)
                    or filename != f'bookmark-focus-{suffix}.json'):
                raise InvalidConfig('invalid bookmark focus result path')
            result_path = self.store.safe('workspaces', name, 'runtime', filename)
            if f'--focus-result-file={result_path}' not in argv:
                raise InvalidConfig('focus result path differs from notification command')
        record = self.store.runtime(name).processes['browser']
        if record is not None and record['marker'] == marker:
            raise InvalidConfig('bookmark notifier cannot be the owned browser')
        pending = process.find_launch(argv, marker)
        owners = process.profile_processes(self.profile(name))
        if len(pending) > 1:
            raise BrowserError('browser_launch_uncertain', 'Multiple bookmark notifier processes; journal retained')
        if pending:
            # Both a short-lived notifier and an unintended new main process are ours,
            # but terminating one must never target the original running browser.
            candidate = pending[0]
            if record is not None and candidate['pid'] == record['pid']:
                raise BrowserError('browser_ownership_unknown', 'Notifier conflicts with owned browser')
            self.terminate(candidate)
            owners = process.profile_processes(self.profile(name))
        if owners and (record is None or process.identity(record) != 'owned' or owners != [record['pid']]):
            raise BrowserError('browser_ownership_unknown', 'Unverified profile owner after bookmark notification')
        if result_path is not None:
            result_path.unlink(missing_ok=True)
        self.journal_path(name).unlink()

    def open_bookmarks(self, name, urls):
        outcomes = []
        # Chrome's comma-separated selectors mean OR, not focus/open each URL.
        # Send one request per bookmark, including duplicate entries in the list.
        for url in urls:
            action = self.notify_bookmark(name, url)
            if action == 'parse_error':
                # Chrome does not open the positional URL on a selector parse error.
                # Retry without the selector only after receiving that explicit result.
                action = self.notify_bookmark(name, url, use_focus=False)
            outcomes.append({'url': url, 'action': action})
        return {'result': 'submitted', 'count': len(urls), 'mode': 'focus_or_open', 'outcomes': outcomes}

    def notify_bookmark(self, name, url, *, use_focus=True):
        self.store.require_writer()
        self.reconcile(name)
        observed = self.observe(name)
        if observed['state'] != 'running':
            raise BrowserError('browser_not_running', 'Start this workspace’s Chrome before opening bookmarks')
        record = self.store.runtime(name).processes['browser']
        marker = f'--devhard-instance={uuid.uuid4().hex}'
        # These have special meaning in Chrome's selector syntax. Never interpret
        # a literal bookmark as multiple selectors or a URL prefix wildcard.
        focus = use_focus and ',' not in url and not url.endswith('*')
        # Preserve proxy safety even if Chrome exits between ownership check and spawn.
        port = self.store.runtime(name).reserved_port
        argv = [record['executable'], f'--user-data-dir={self.profile(name)}',
                f'--proxy-server=socks5://127.0.0.1:{port}', '--proxy-bypass-list=<-loopback>',
                '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                marker, '--new-tab']
        journal = self.journal_path(name)
        raw = {'schema_version': 1, 'purpose': 'open_bookmarks', 'argv': argv, 'marker': marker}
        result_path = None
        if focus:
            filename = f'bookmark-focus-{marker.split("=", 1)[1]}.json'
            result_path = self.store.safe('workspaces', name, 'runtime', filename)
            # GetSwitchValueASCII is used by Chrome. Percent-encode Unicode, but
            # preserve existing escapes and URL structure for GURL matching.
            selector = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
            argv += [f'--focus={selector}', f'--focus-result-file={result_path}']
            raw['focus_result'] = filename
        argv.append(url)  # Native fallback when no existing tab matches.
        atomic_write(journal, json_text(raw))
        child = None
        try:
            if result_path is not None:
                atomic_write(result_path, '')  # Owner-only permissions, fresh per request.
            log_path = self.store.safe('workspaces', name, 'runtime', 'browser.log')
            fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab', buffering=0) as log:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, close_fds=True)
            code = child.wait(timeout=self.start_timeout)
            payload = None
            if result_path is not None:
                try:
                    payload = self.store.read(result_path)
                except (OSError, InvalidConfig):
                    pass  # Older Chrome can ignore the switch and open the URL normally.
            self.reconcile_open(name, raw)
            if code != 0 or process.identity(record) != 'owned' or self.observe(name)['state'] != 'running':
                raise BrowserError('bookmark_open_failed', 'Chrome did not accept bookmarks in the original instance')
            if not focus:
                return 'opened_without_focus'
            status = payload.get('status') if isinstance(payload, dict) else None
            if status == 'focused':
                return 'focused'
            if status in ('no_match', 'opened_fallback'):
                # no_match JSON exit_code=1 is normal; the notifier itself exits 0.
                return 'opened'
            if status == 'parse_error':
                return 'parse_error'
            # Do not resend on a missing report: a new tab may already be open.
            return 'unconfirmed'
        except BaseException as error:
            if journal.exists():
                self.reconcile_open(name, raw)
            if child is not None:
                child.poll()
            if isinstance(error, subprocess.TimeoutExpired):
                raise BrowserError('bookmark_open_timeout', 'Chrome did not acknowledge the bookmark request in time') from error
            raise

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
