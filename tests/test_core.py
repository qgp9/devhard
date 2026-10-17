import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from devhard.config import DevHardError, GlobalConfig, InvalidConfig, RuntimeState, WorkspaceConfig
from devhard.manager import AlreadyRunning, Manager, status
from devhard.store import Store, atomic_write


def config(workspace_name='dev-a', **values):
    raw = {'schema_version': 1, 'name': workspace_name, 'ssh': {'host': 'dev-a'},
           'tabs': ['http://127.0.0.1:3000', 'https://example.test/경로']}
    raw.update(values)
    return WorkspaceConfig.parse(raw)


class SchemaTests(unittest.TestCase):
    def test_reject_path_names(self):
        for value in ('../outside', '/tmp/outside', '..', '.', 'A', 'a/b', 'a\\b', '', 'a' * 64):
            with self.subTest(name=value), self.assertRaises(InvalidConfig):
                config(value)

    def test_reject_ssh_options_and_commands(self):
        for host in ('-oProxyCommand=whoami', 'host name', 'host;whoami', 'host\n', ''):
            with self.subTest(host=host), self.assertRaises(InvalidConfig):
                config(ssh={'host': host})
        self.assertEqual(config(ssh={'host': 'user@dev1.h'}).ssh_host, 'user@dev1.h')

    def test_bad_tabs_and_browser_paths(self):
        for values in ({'tabs': ['file:///etc/passwd']}, {'tabs': ['http://localhost:99999']},
                       {'tabs': ['http://localhost:0']}, {'tabs': ['http://local host']},
                       {'tabs': ['http://[broken']}, {'tabs': [False]},
                       {'browser': {'profile': '../../outside'}},
                       {'browser': {'restore_session': 'yes'}}):
            with self.subTest(values=values), self.assertRaises(InvalidConfig):
                config(**values)

    def test_future_versions_and_unknown_fields_rejected(self):
        for values in ({'schema_version': 2}, {'schema_version': True}, {'typo': 1}, {'ssh': {'hst': 'host'}}):
            with self.subTest(values=values), self.assertRaises(InvalidConfig):
                config(**values)

    def test_global_port_range_validation(self):
        for ports in ({'min': 61000, 'max': 60000}, {'min': 80}, {'max': 65536}, {'min': True}):
            with self.subTest(ports=ports), self.assertRaises(InvalidConfig):
                GlobalConfig.parse({'schema_version': 1, 'ports': ports})

    def test_invalid_runtime_rejected(self):
        for values in ({'reserved_port': True}, {'reserved_port': 65536}, {'schema_version': 2},
                       {'processes': {'tunnel': {'pid': 3}, 'browser': None}},
                       {'processes': {'tunnel': None}}):
            raw = RuntimeState().as_dict()
            raw.update(values)
            with self.subTest(values=values), self.assertRaises(InvalidConfig):
                RuntimeState.parse(raw)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'data')

    def test_read_only_commands_do_not_create_tree(self):
        self.assertEqual(self.store.workspaces(), [])
        self.assertFalse(status(self.store)['active'])
        self.assertFalse(self.store.root.exists())

    def test_registration_roundtrip_and_duplicate(self):
        original = config()
        with Manager(self.store) as manager:
            manager.register(original)
            with self.assertRaisesRegex(DevHardError, 'already exists'):
                manager.register(original)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.workspaces(), [original])
        self.assertEqual(reopened.runtime('dev-a'), RuntimeState())
        self.assertTrue(reopened.safe('workspaces', 'dev-a', 'chrome-profile').is_dir())

    def test_writes_require_manager_lock(self):
        with self.assertRaisesRegex(DevHardError, 'Manager lock'):
            self.store.register(config())

    def test_preserve_profile_reservation_and_process_records(self):
        process = {'pid': 123456, 'started': 'opaque OS identity', 'executable': '/usr/bin/ssh',
                   'argv': ['ssh', '-N'], 'marker': 'workspace-unique-marker'}
        with Manager(self.store) as manager:
            manager.register(config())
            self.store.save_runtime('dev-a', RuntimeState(60001, {'tunnel': process, 'browser': None}))
        profile_file = self.store.safe('workspaces', 'dev-a', 'chrome-profile', 'user-data')
        profile_file.write_bytes(b'keep me')
        runtime_path = self.store.safe('workspaces', 'dev-a', 'runtime', 'state.json')
        original = runtime_path.read_bytes()
        with Manager(Store(self.store.root)) as manager:
            self.assertTrue(manager.ready)
            self.assertEqual(manager.restored[0]['status'], {'tunnel': 'unknown', 'browser': 'stopped'})
            self.assertEqual(manager.restored[0]['runtime']['reserved_port'], 60001)
        self.assertEqual(runtime_path.read_bytes(), original)
        self.assertEqual(profile_file.read_bytes(), b'keep me')

    def test_symlink_escape_rejected(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        with Manager(self.store) as manager:
            manager.register(config())
        profile = self.store.safe('workspaces', 'dev-a', 'chrome-profile')
        profile.rmdir()
        profile.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(InvalidConfig, 'symlink'):
            with Manager(self.store):
                pass
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(status(self.store)['active'])

    def test_runtime_directory_symlink_rejected(self):
        self.store.root.mkdir()
        external = Path(self.temporary.name) / 'external'
        external.mkdir()
        (self.store.root / 'runtime').symlink_to(external, target_is_directory=True)
        with self.assertRaises(InvalidConfig):
            with Manager(self.store):
                pass
        self.assertEqual(list(external.iterdir()), [])

    def test_global_config_symlink_rejected(self):
        with Manager(self.store):
            pass
        external = Path(self.temporary.name) / 'external.toml'
        external.write_text('schema_version = 1\n')
        target = self.store.safe('config.toml')
        target.unlink()
        target.symlink_to(external)
        with self.assertRaises(InvalidConfig):
            with Manager(self.store):
                pass
        self.assertEqual(external.read_text(), 'schema_version = 1\n')
        self.assertFalse(status(self.store)['active'])

    def test_corrupt_manager_metadata_does_not_hold_lock(self):
        with Manager(self.store):
            pass
        self.store.safe('runtime', 'manager.json').write_text('{invalid')
        self.assertFalse(status(self.store)['active'])
        with Manager(self.store) as manager:
            self.assertTrue(manager.ready)

    def test_root_aliases_share_lock(self):
        with Manager(self.store):
            alias = Path(self.temporary.name) / 'alias'
            alias.symlink_to(self.store.root, target_is_directory=True)
            with self.assertRaises(AlreadyRunning):
                with Manager(Store(alias)):
                    pass

    def test_bad_runtime_prevents_ready_and_releases_lock(self):
        with Manager(self.store) as manager:
            manager.register(config())
        path = self.store.safe('workspaces', 'dev-a', 'runtime', 'state.json')
        path.write_text('{broken')
        original = path.read_bytes()
        with self.assertRaises(InvalidConfig):
            with Manager(self.store):
                self.fail('should not become ready')
        self.assertFalse(status(self.store)['active'])
        self.assertEqual(path.read_bytes(), original)

    def test_duplicate_reservations_prevent_ready(self):
        with Manager(self.store) as manager:
            for value in ('dev-a', 'dev-b'):
                manager.register(config(value))
                self.store.save_runtime(value, RuntimeState(reserved_port=60001))
        with self.assertRaisesRegex(InvalidConfig, 'duplicate reserved port'):
            with Manager(self.store):
                pass

    def test_missing_runtime_does_not_assume_stopped(self):
        with Manager(self.store) as manager:
            manager.register(config())
        self.store.safe('workspaces', 'dev-a', 'runtime', 'state.json').unlink()
        with self.assertRaises(FileNotFoundError):
            with Manager(self.store):
                pass
        self.assertFalse(status(self.store)['active'])

    def test_atomic_write_failure_keeps_original(self):
        path = Path(self.temporary.name) / 'state.json'
        path.write_text('old')
        with patch('devhard.store.os.replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                atomic_write(path, 'new')
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(list(path.parent.glob('.state.json-*')), [])

    def test_incomplete_registration_is_not_published(self):
        with Manager(self.store) as manager:
            with patch('devhard.store.workspace_toml', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    manager.register(config())
            self.assertEqual(self.store.workspaces(), [])
            self.assertEqual(list(self.store.safe('workspaces').iterdir()), [])


class ProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        self.store = Store(self.root)
        self.base = [sys.executable, '-m', 'devhard', '--data-dir', str(self.root)]

    def cli(self, *args):
        return subprocess.run(self.base + list(args), capture_output=True, text=True, timeout=10)

    def start(self):
        output = tempfile.TemporaryFile(mode='w+')
        self.addCleanup(output.close)
        proc = subprocess.Popen(self.base + ['manager', 'run'], stdout=output, stderr=output)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output.seek(0)
                self.fail(output.read())
            metadata = self.root / 'runtime' / 'manager.json'
            if metadata.exists():
                info = json.loads(metadata.read_text())
                if info.get('pid') == proc.pid and info.get('phase') == 'ready':
                    return proc, info
            time.sleep(.02)
        self.fail('manager did not become ready')

    def test_graceful_and_forced_restart_with_stale_metadata(self):
        added = self.cli('workspace', 'add', 'dev-a', '--ssh-host', 'dev-a', '--tab', 'http://127.0.0.1:3000')
        self.assertEqual(added.returncode, 0, added.stderr)
        with Manager(self.store):
            self.store.save_runtime('dev-a', RuntimeState(reserved_port=60001))
        profile_file = self.root / 'workspaces' / 'dev-a' / 'chrome-profile' / 'marker'
        profile_file.write_text('keep')
        first, info1 = self.start()
        self.assertTrue(json.loads(self.cli('manager', 'status').stdout)['active'])
        rejected = self.cli('manager', 'run')
        self.assertEqual(rejected.returncode, 73, rejected.stderr)
        self.assertEqual(self.cli('workspace', 'add', 'dev-b', '--ssh-host', 'dev-b').returncode, 73)
        self.assertEqual(len(json.loads(self.cli('workspace', 'list').stdout)), 1)
        first.terminate()
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertFalse(status(self.store)['active'])
        second, info2 = self.start()
        self.assertNotEqual(info1['instance_id'], info2['instance_id'])
        second.kill()
        second.wait(timeout=5)
        observed = status(self.store)
        self.assertFalse(observed['active'])
        self.assertEqual(observed['last_run']['phase'], 'ready')  # stale metadata is not liveness
        third, info3 = self.start()
        self.assertNotEqual(info2['instance_id'], info3['instance_id'])
        self.assertEqual(info3['restored_count'], 1)
        third.terminate()
        self.assertEqual(third.wait(timeout=5), 0)
        self.assertEqual(self.store.runtime('dev-a').reserved_port, 60001)
        self.assertEqual(profile_file.read_text(), 'keep')

    def test_simultaneous_registration_is_serialized_or_rejected(self):
        argv = self.base + ['workspace', 'add', 'same', '--ssh-host', 'dev-a']
        first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for proc in (first, second):
            proc.communicate(timeout=10)
        self.assertEqual(sorted([first.returncode, second.returncode])[0], 0)
        self.assertIn(sorted([first.returncode, second.returncode])[1], [1, 73])
        self.assertEqual([item.name for item in self.store.workspaces()], ['same'])
        self.assertEqual(self.store.runtime('same'), RuntimeState())

    def test_child_exec_does_not_keep_manager_lock(self):
        child = None
        try:
            with Manager(self.store):
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                         close_fds=False, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertIsNone(child.poll())
            with Manager(Store(self.root)) as manager:
                self.assertTrue(manager.ready)
        finally:
            if child is not None:
                child.terminate()
                child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
