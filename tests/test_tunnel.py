from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from devhard import process
from devhard.config import RuntimeState, WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text
from devhard.tunnel import TunnelController, TunnelError, classify


FIXTURE = Path(__file__).parent / 'fixtures' / 'tunnel_child.py'
PYTHON = str(Path(sys.executable).resolve())


def record(marker='DEVHARD_INSTANCE=test', argv=None):
    argv = argv or ['/usr/bin/ssh', '-N', '-o', f'SetEnv={marker}']
    return {'pid': 123456, 'started': 'macos:1:2', 'executable': argv[0], 'argv': argv, 'marker': marker}


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.manager = Manager(self.store).__enter__()
        self.addCleanup(self.manager.__exit__, None, None, None)
        for name in ('a', 'b'):
            self.manager.register(WorkspaceConfig(name, 'host'))
        self.controller = self.manager.tunnels

    def test_distinct_reservations_survive_manager_reopen(self):
        with patch('devhard.tunnel.available', return_value=True):
            first = self.controller.reserve('a')
            second = self.controller.reserve('b')
        self.assertNotEqual(first.reserved_port, second.reserved_port)
        self.manager.__exit__(None, None, None)
        with Manager(Store(self.store.root)) as manager:
            self.assertEqual(manager.store.runtime('a').reserved_port, first.reserved_port)

    def test_exhausted_and_busy_ranges_do_not_escape_range(self):
        atomic_write(self.store.safe('config.toml'), 'schema_version = 1\n[ports]\nmin = 60000\nmax = 60000\n')
        self.store.save_runtime('b', RuntimeState(reserved_port=60000))
        with patch('devhard.tunnel.available') as available:
            with self.assertRaises(TunnelError) as failure:
                self.controller.reserve('a')
            self.assertEqual(failure.exception.code, 'port_range_exhausted')
            available.assert_not_called()
        self.store.save_runtime('b', RuntimeState())
        with patch('devhard.tunnel.available', return_value=False):
            with self.assertRaises(TunnelError) as failure:
                self.controller.reserve('a')
            self.assertEqual(failure.exception.code, 'port_range_exhausted')
        self.assertIsNone(self.store.runtime('a').reserved_port)

    def test_old_reservation_is_preserved_after_range_change(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=59000))
        with patch('devhard.tunnel.available') as available:
            self.assertEqual(self.controller.reserve('a').reserved_port, 59000)
            available.assert_not_called()

    def test_reserved_port_conflict_does_not_launch_or_reallocate(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=60001))
        with patch('devhard.tunnel.available', return_value=False), patch.object(self.controller, 'launch') as launch:
            with self.assertRaises(TunnelError) as failure:
                self.controller.start('a')
            self.assertEqual(failure.exception.code, 'port_conflict')
            launch.assert_not_called()
        state = self.store.runtime('a')
        self.assertEqual(state.reserved_port, 60001)
        self.assertEqual(state.last_error['code'], 'port_conflict')

    def test_only_initial_binding_collision_retries(self):
        ports = []

        def collided(name, state, argv, marker):
            ports.append(state.reserved_port)
            raise TunnelError('port_conflict', 'Address already in use')

        with patch('devhard.tunnel.available', return_value=True), patch.object(self.controller, 'launch', side_effect=collided):
            with self.assertRaises(TunnelError):
                self.controller.start('a')
        self.assertEqual(len(ports), 3)
        self.assertEqual(len(set(ports)), 3)
        self.assertEqual(self.store.runtime('a').reserved_port, ports[-1])
        ports.clear()
        with patch('devhard.tunnel.available', return_value=True), patch.object(self.controller, 'launch', side_effect=collided):
            with self.assertRaises(TunnelError):
                self.controller.start('a')
        self.assertEqual(len(ports), 1)

    def test_auth_failure_does_not_retry_or_discard_reservation(self):
        with patch('devhard.tunnel.available', return_value=True), \
             patch.object(self.controller, 'launch', side_effect=TunnelError('authentication_failed', 'Permission denied')) as launch:
            with self.assertRaises(TunnelError):
                self.controller.start('a')
            launch.assert_called_once()
        self.assertIsNotNone(self.store.runtime('a').reserved_port)
        self.assertEqual(self.store.runtime('a').last_error['code'], 'authentication_failed')

    def test_stop_retains_port_and_stop_all_releases(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=60001))
        self.controller.stop('a')
        self.assertEqual(self.store.runtime('a').reserved_port, 60001)
        self.controller.stop_all('a')
        self.assertIsNone(self.store.runtime('a').reserved_port)

    def test_browser_record_prevents_reservation_release(self):
        self.store.save_runtime('a', RuntimeState(60001, {'tunnel': None, 'browser': record()}))
        with self.assertRaises(TunnelError) as failure:
            self.controller.stop_all('a')
        self.assertEqual(failure.exception.code, 'browser_not_stopped')
        self.assertEqual(self.store.runtime('a').reserved_port, 60001)

    def test_unknown_process_is_never_signalled(self):
        self.store.save_runtime('a', RuntimeState(60001, {'tunnel': record(), 'browser': None}))
        with patch('devhard.process.identity', return_value='unknown'), patch('devhard.tunnel.os.kill') as kill:
            with self.assertRaises(TunnelError) as failure:
                self.controller.stop('a')
            self.assertEqual(failure.exception.code, 'ownership_unknown')
            kill.assert_not_called()
        self.assertIsNotNone(self.store.runtime('a').processes['tunnel'])

    def test_exit_time_command_change_waits_for_absence_without_resignalling(self):
        self.store.save_runtime('a', RuntimeState(60001, {'tunnel': record(), 'browser': None}))
        with patch('devhard.process.identity', side_effect=['owned', 'owned', 'unknown', 'exited']), \
             patch('devhard.tunnel.os.kill') as kill, patch('devhard.tunnel.time.sleep'):
            self.controller.stop('a')
            kill.assert_called_once()
        self.assertIsNone(self.store.runtime('a').processes['tunnel'])
        self.assertEqual(self.store.runtime('a').reserved_port, 60001)

    def test_journal_recovers_one_verified_process(self):
        state = RuntimeState(reserved_port=60001)
        self.store.save_runtime('a', state)
        saved = record()
        path = self.controller.journal_path('a')
        atomic_write(path, json_text({'schema_version': 1, 'marker': saved['marker'],
                                     'argv': saved['argv'], 'reserved_port': 60001}))
        with patch('devhard.process.find_launch', return_value=[saved]):
            self.controller.reconcile('a')
        self.assertFalse(path.exists())
        self.assertEqual(self.store.runtime('a').processes['tunnel'], saved)

    def test_exit_time_inspection_failure_waits_for_verified_absence(self):
        self.store.save_runtime('a', RuntimeState(60001, {'tunnel': record(), 'browser': None}))
        with patch('devhard.process.identity', side_effect=[
                'owned', 'owned', process.InspectionError('exiting process'), 'exited']), \
             patch('devhard.tunnel.os.kill') as kill, patch('devhard.tunnel.time.sleep'):
            self.controller.stop('a')
            kill.assert_called_once()
        self.assertIsNone(self.store.runtime('a').processes['tunnel'])

    def test_uncertain_journal_prevents_duplicate_launch(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=60001))
        saved = record()
        path = self.controller.journal_path('a')
        atomic_write(path, json_text({'schema_version': 1, 'marker': saved['marker'],
                                     'argv': saved['argv'], 'reserved_port': 60001}))
        with patch('devhard.process.find_launch', return_value=[]), patch.object(self.controller, 'launch') as launch:
            with self.assertRaises(TunnelError) as failure:
                self.controller.start('a')
            self.assertEqual(failure.exception.code, 'launch_uncertain')
            launch.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual(self.controller.observe('a')['state'], 'unknown')

    def test_pid_and_command_alone_do_not_establish_ownership(self):
        saved = record()
        current = {'pid': saved['pid'], 'started': 'macos:9:9',
                   'executable': saved['executable'], 'command': ' '.join(saved['argv'])}
        self.assertFalse(process.matches(saved, current))
        current['started'] = saved['started']
        self.assertTrue(process.matches(saved, current))
        current['executable'] = '/usr/bin/another-program'
        self.assertFalse(process.matches(saved, current))

    def test_error_classification(self):
        for message, code in (
            ('Permission denied (publickey).', 'authentication_failed'),
            ('Host key verification failed.', 'host_key_verification'),
            ('REMOTE HOST IDENTIFICATION HAS CHANGED!', 'host_key_changed'),
            ('bind: Address already in use', 'port_conflict'),
            ('Could not resolve hostname missing.invalid', 'dns_failed'),
            ('connect: Connection refused', 'connection_failed'),
            ('Connection timed out', 'connection_timeout'),
            ('unexpected EOF', 'ssh_exited'),
        ):
            with self.subTest(message=message):
                self.assertEqual(classify(message), code)


class LocalProcessTests(unittest.TestCase):
    """Actual OS processes/listeners; no external SSH credentials or network needed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.manager = Manager(self.store).__enter__()
        self.controller = self.manager.tunnels
        self.controller.start_timeout = 1.5
        self.manager.register(WorkspaceConfig('a', 'host'))
        self.mode = 'socks'
        self.message = ''
        self.command_patch = patch.object(self.controller, 'ssh_command', side_effect=self.command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.addCleanup(self.cleanup)

    def command(self, host, port, marker):
        if self.mode == 'fail':
            args = ['fail', self.message]
        else:
            args = [self.mode, str(port)]
        return [PYTHON, str(FIXTURE.resolve()), *args, f'SetEnv={marker}']

    def cleanup(self):
        try:
            self.controller.stop_all('a')
        finally:
            self.manager.__exit__(None, None, None)

    def test_real_listener_owned_by_child_and_reused_after_stop(self):
        first = self.controller.start('a')
        self.assertEqual(first['state'], 'listening')
        self.assertTrue(first['listener_owned'])
        saved = self.store.runtime('a').processes['tunnel']
        self.assertEqual(process.identity(saved), 'owned')
        with patch('devhard.process.snapshot', return_value={
                **process.snapshot(saved['pid']), 'started': 'macos:0:0'}):
            self.assertEqual(process.identity(saved), 'unknown')
        self.controller.stop('a')
        self.assertEqual(self.store.runtime('a').reserved_port, first['port'])
        second = self.controller.start('a')
        self.assertEqual(first['port'], second['port'])
        self.assertNotEqual(saved['pid'], self.store.runtime('a').processes['tunnel']['pid'])

    def test_no_owned_listener_cannot_pass_from_foreign_socks_response(self):
        self.mode = 'hang'
        self.controller.start_timeout = .2
        with patch('devhard.process.owns_listener', return_value=False), \
             patch('devhard.tunnel.socks_ready', return_value=True) as socks:
            with self.assertRaises(TunnelError) as failure:
                self.controller.start('a')
            self.assertEqual(failure.exception.code, 'start_timeout')
            socks.assert_not_called()
        self.assertIsNone(self.store.runtime('a').processes['tunnel'])
        self.assertFalse(self.controller.journal_path('a').exists())

    def test_actual_early_exit_reports_current_error_not_old_log(self):
        self.mode = 'fail'
        log = self.store.safe('workspaces', 'a', 'runtime', 'tunnel.log')
        log.write_text('Permission denied (publickey).\n')
        self.message = 'Could not resolve hostname unavailable.invalid'
        with self.assertRaises(TunnelError) as failure:
            self.controller.start('a')
        self.assertEqual(failure.exception.code, 'dns_failed')
        self.assertIsNone(self.store.runtime('a').processes['tunnel'])
        self.assertFalse(self.controller.journal_path('a').exists())

    def test_real_process_is_recovered_from_launch_journal(self):
        state = self.controller.reserve('a')
        marker = f'DEVHARD_INSTANCE={uuid.uuid4().hex}'
        argv = self.command('host', state.reserved_port, marker)
        atomic_write(self.controller.journal_path('a'), json_text({
            'schema_version': 1, 'argv': argv, 'marker': marker, 'reserved_port': state.reserved_port}))
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while not process.owns_listener(child.pid, state.reserved_port):
                if child.poll() is not None or time.monotonic() > deadline:
                    self.fail('fixture listener did not start')
                time.sleep(.02)
            self.assertIsNone(self.store.runtime('a').processes['tunnel'])
            self.manager.__exit__(None, None, None)
            self.manager = Manager(self.store).__enter__()
            self.controller = self.manager.tunnels
            self.assertEqual(self.store.runtime('a').processes['tunnel']['pid'], child.pid)
            self.assertFalse(self.controller.journal_path('a').exists())
            self.assertEqual(self.controller.observe('a')['state'], 'listening')
            self.controller.stop('a')
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)

    def test_launch_record_write_failure_stops_created_process(self):
        original = self.store.save_runtime
        created = []
        original_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            created.append(child)
            return child

        def save(name, state):
            if state.processes['tunnel'] is not None:
                raise OSError('injected runtime write failure')
            return original(name, state)

        # Only Popen for the fixture is tracked; ps/lsof also use subprocess.Popen.
        def tracked(*args, **kwargs):
            if str(FIXTURE.resolve()) in args[0]:
                return launch(*args, **kwargs)
            return original_popen(*args, **kwargs)

        with patch.object(self.store, 'save_runtime', side_effect=save), patch('subprocess.Popen', side_effect=tracked):
            with self.assertRaises(TunnelError) as failure:
                self.controller.start('a')
            self.assertEqual(failure.exception.code, 'launch_failed')
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll())
        self.assertFalse(self.controller.journal_path('a').exists())


if __name__ == '__main__':
    unittest.main()
