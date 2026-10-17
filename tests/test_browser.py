from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from devhard import process
from devhard.browser import BrowserError
from devhard.config import RuntimeState, WorkspaceConfig, InvalidConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text
from devhard.tunnel import TunnelError


FIXTURE = Path(__file__).parent / 'fixtures' / 'tunnel_child.py'
PYTHON = str(Path(sys.executable).resolve())


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.manager = Manager(self.store).__enter__()
        self.manager.register(WorkspaceConfig('a', 'host', ('http://127.0.0.1:3000',)))
        self.browser = self.manager.browsers
        self.addCleanup(self.manager.__exit__, None, None, None)

    def test_first_launch_urls_then_restore_without_duplicate_urls(self):
        config = self.store.workspace('a')
        with patch.object(self.browser, 'executable', return_value='/chrome'):
            first = self.browser.command(config, RuntimeState(reserved_port=60001), '--devhard-instance=test')
            restored = self.browser.command(config, RuntimeState(reserved_port=60001, browser_initialized=True), '--devhard-instance=test')
            explicit = self.browser.command(replace(config, restore_session=False), RuntimeState(reserved_port=60001, browser_initialized=True), '--devhard-instance=test')
        self.assertIn(config.tabs[0], first)
        self.assertNotIn('--restore-last-session', first)
        self.assertIn('--restore-last-session', restored)
        self.assertNotIn(config.tabs[0], restored)
        self.assertNotIn(config.tabs[0], explicit)
        self.assertIn('about:blank', explicit)
        self.assertFalse(any(arg.startswith('--remote-debugging') for arg in first))

    def test_invalid_initialized_flag_is_rejected(self):
        raw = RuntimeState().as_dict()
        raw['browser_initialized'] = 'yes'
        with self.assertRaises(InvalidConfig):
            RuntimeState.parse(raw)

    def test_missing_browser_leaves_tunnel_as_partial_success(self):
        with patch.object(self.manager.tunnels, 'start', return_value={'state': 'listening'}), \
             patch.object(self.browser, 'start', side_effect=BrowserError('browser_missing', 'missing executable')), \
             patch.object(self.manager.tunnels, 'stop') as stop:
            result = self.manager.workspace_action('a', 'start_all')
        self.assertEqual(result['result'], 'partial')
        self.assertEqual(result['error']['code'], 'browser_missing')
        stop.assert_not_called()

    def test_tunnel_failure_does_not_start_browser(self):
        with patch.object(self.manager.tunnels, 'start', side_effect=TunnelError('authentication_failed', 'denied')), \
             patch.object(self.browser, 'start') as start:
            with self.assertRaises(TunnelError):
                self.manager.workspace_action('a', 'start_all')
            start.assert_not_called()

    def test_unmanaged_profile_never_signalled_or_duplicated(self):
        with patch('devhard.process.profile_processes', return_value=[999]), \
             patch('devhard.browser.os.kill') as kill, patch.object(self.browser, 'launch') as launch:
            for action in ('start', 'stop'):
                with self.assertRaises(BrowserError) as failure:
                    getattr(self.browser, action)('a')
                self.assertEqual(failure.exception.code, 'browser_ownership_unknown')
            kill.assert_not_called()
            launch.assert_not_called()
        self.assertIsNone(self.store.runtime('a').reserved_port)

    def test_stop_all_refusal_preserves_tunnel_and_reservation(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=60001))
        with patch.object(self.browser, 'stop', side_effect=BrowserError('browser_ownership_unknown', 'unverified')), \
             patch.object(self.manager.tunnels, 'stop_all') as stop:
            with self.assertRaises(BrowserError):
                self.manager.workspace_action('a', 'stop_all')
            stop.assert_not_called()
        self.assertEqual(self.store.runtime('a').reserved_port, 60001)

    def test_journal_without_unique_process_prevents_new_launch(self):
        self.store.save_runtime('a', RuntimeState(reserved_port=60001))
        marker = '--devhard-instance=test'
        argv = ['/chrome', f'--user-data-dir={self.browser.profile("a")}',
                '--proxy-server=socks5://127.0.0.1:60001', marker]
        journal = self.browser.journal_path('a')
        atomic_write(journal, json_text({'schema_version': 1, 'argv': argv, 'marker': marker, 'reserved_port': 60001}))
        with patch('devhard.process.find_launch', return_value=[]), patch.object(self.browser, 'launch') as launch:
            with self.assertRaises(BrowserError) as failure:
                self.browser.start('a')
            self.assertEqual(failure.exception.code, 'browser_launch_uncertain')
            launch.assert_not_called()
        self.assertTrue(journal.exists())

    def test_profile_lock_blocks_alias_user_without_deleting_lock(self):
        lock = self.browser.profile('a') / 'SingletonLock'
        lock.symlink_to(f'{socket.gethostname()}-123456')
        with patch('devhard.process.profile_processes', return_value=[]), \
             patch('devhard.process.snapshot', return_value={'pid': 123456}), \
             patch.object(self.browser, 'launch') as launch:
            with self.assertRaises(BrowserError) as failure:
                self.browser.start('a')
            self.assertEqual(failure.exception.code, 'browser_ownership_unknown')
            launch.assert_not_called()
        self.assertTrue(lock.is_symlink())

    def test_proven_stale_local_profile_lock_is_left_for_chrome(self):
        lock = self.browser.profile('a') / 'SingletonLock'
        lock.symlink_to(f'{socket.gethostname()}-123456')
        with patch('devhard.process.profile_processes', return_value=[]), \
             patch('devhard.process.snapshot', return_value=None):
            self.assertEqual(self.browser.observe('a')['state'], 'stopped')
        self.assertTrue(lock.is_symlink())

    def test_normal_application_quit_does_not_send_sigterm(self):
        with patch('devhard.process.identity', side_effect=['owned', 'owned', 'exited']), \
             patch('devhard.process.terminate_application', return_value=True), \
             patch('devhard.browser.os.kill') as kill:
            self.browser.terminate({'pid': 123456})
            kill.assert_not_called()

    def test_rejected_application_quit_does_not_force_signal(self):
        with patch('devhard.process.identity', side_effect=['owned', 'owned']), \
             patch('devhard.process.terminate_application', side_effect=process.InspectionError('quit denied')), \
             patch('devhard.browser.os.kill') as kill:
            with self.assertRaises(process.InspectionError):
                self.browser.terminate({'pid': 123456})
            kill.assert_not_called()


class BrowserProcessTests(unittest.TestCase):
    """Use disposable native Python processes; actual Chrome is verified by browser_probe."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.manager = Manager(self.store).__enter__()
        self.manager.register(WorkspaceConfig('a', 'host'))
        self.browser = self.manager.browsers
        self.command_patch = patch.object(self.browser, 'command', side_effect=self.command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.addCleanup(self.cleanup)

    def command(self, config, state, marker):
        return [PYTHON, str(FIXTURE.resolve()), 'hang', '0',
                f'--user-data-dir={self.browser.profile(config.name)}',
                f'--proxy-server=socks5://127.0.0.1:{state.reserved_port}', marker]

    def cleanup(self):
        try:
            self.browser.stop('a')
        finally:
            self.manager.__exit__(None, None, None)

    def test_independent_browser_reservation_and_concurrent_start(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.manager.browser_action('a', 'start'), range(2)))
        self.assertTrue(all(result['state'] == 'running' for result in results))
        state = self.store.runtime('a')
        self.assertIsNone(state.processes['tunnel'])
        self.assertTrue(state.browser_initialized)
        self.assertEqual(process.profile_processes(self.browser.profile('a')), [state.processes['browser']['pid']])
        self.browser.stop('a')
        self.assertEqual(self.store.runtime('a').reserved_port, state.reserved_port)

    def test_reused_pid_identity_is_not_signalled(self):
        self.browser.start('a')
        original = self.store.runtime('a')
        saved = original.processes['browser']
        self.store.save_runtime('a', replace(original, processes={**original.processes, 'browser': {**saved, 'started': 'macos:0:0'}}))
        try:
            with self.assertRaises(BrowserError):
                self.browser.stop('a')
            self.assertEqual(process.identity(saved), 'owned')
        finally:
            self.store.save_runtime('a', original)

    def test_normal_manager_exit_then_adopt_same_browser(self):
        self.browser.start('a')
        saved = self.store.runtime('a').processes['browser']
        self.manager.__exit__(None, None, None)
        self.assertEqual(process.identity(saved), 'owned')
        self.manager = Manager(self.store).__enter__()
        self.browser = self.manager.browsers
        self.assertEqual(self.browser.start('a')['state'], 'running')
        self.assertEqual(self.store.runtime('a').processes['browser']['pid'], saved['pid'])

    def test_record_write_failure_cleans_new_browser(self):
        original = self.store.save_runtime
        def fail_record(name, state):
            if state.processes['browser'] is not None:
                raise OSError('injected write failure')
            return original(name, state)
        with patch.object(self.store, 'save_runtime', side_effect=fail_record):
            with self.assertRaises(BrowserError):
                self.browser.start('a')
        self.assertEqual(process.profile_processes(self.browser.profile('a')), [])
        self.assertFalse(self.browser.journal_path('a').exists())

    def test_stop_all_releases_port_and_preserves_initialized_session(self):
        self.browser.start('a')
        result = self.manager.workspace_action('a', 'stop_all')
        self.assertEqual(result['result'], 'success')
        state = self.store.runtime('a')
        self.assertIsNone(state.reserved_port)
        self.assertTrue(state.browser_initialized)
        self.assertTrue(self.browser.profile('a').is_dir())


if __name__ == '__main__':
    unittest.main()
