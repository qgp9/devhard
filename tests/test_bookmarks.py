from dataclasses import replace
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from devhard.browser import BrowserError
from devhard.config import Bookmark, InvalidConfig, RuntimeState, WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text, workspace_toml


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name)
        self.manager = Manager(self.store).__enter__()
        self.addCleanup(self.manager.__exit__, None, None, None)
        self.config = WorkspaceConfig('demo', 'host', bookmarks=(
            Bookmark('App', 'http://127.0.0.1:3000', 'Development app', True),
            Bookmark('Docs', 'http://127.0.0.1:6767', 'API documentation')))
        self.manager.register(self.config)
        self.browser = self.manager.browsers

    def test_legacy_tabs_and_named_bookmarks_roundtrip(self):
        raw = {'schema_version': 1, 'name': 'legacy', 'ssh': {'host': 'host'},
               'tabs': ['http://127.0.0.1:3000']}
        config = WorkspaceConfig.parse(raw)
        self.assertEqual(config.bookmarks[0], Bookmark(raw['tabs'][0], raw['tabs'][0], initial=True))
        self.assertNotIn('tabs', config.as_dict())
        for original in (config, self.config, WorkspaceConfig('empty', 'host')):
            self.assertEqual(WorkspaceConfig.parse(tomllib.loads(workspace_toml(original))), original)
        self.assertEqual(self.config.tabs, ('http://127.0.0.1:3000',))
        emptied = replace(self.config, bookmarks=())
        self.assertEqual(emptied.tabs, ())
        self.assertEqual(WorkspaceConfig.parse(tomllib.loads(workspace_toml(emptied))), emptied)

    def test_invalid_bookmark_metadata_rejected(self):
        for values in ({'url': 'javascript:alert(1)'}, {'name': ''}, {'name': 'a\nb'},
                       {'initial': 'yes'}, {'description': 1}, {'description': '\0'}):
            bookmark = {**self.config.bookmarks[0].as_dict(), **values}
            with self.subTest(values=values), self.assertRaises(InvalidConfig):
                WorkspaceConfig.parse({**self.config.as_dict(), 'bookmarks': [bookmark]})
        with self.assertRaises(InvalidConfig):
            WorkspaceConfig.parse({**self.config.as_dict(), 'tabs': []})

    def test_initial_is_once_and_manual_bookmarks_never_autolaunch(self):
        with patch.object(self.browser, 'executable', return_value='/chrome'):
            first = self.browser.command(self.config, RuntimeState(reserved_port=60000), 'marker')
            resumed = self.browser.command(self.config, RuntimeState(reserved_port=60000, browser_initialized=True), 'marker')
            blank = self.browser.command(replace(self.config, restore_session=False),
                                         RuntimeState(reserved_port=60000, browser_initialized=True), 'marker')
        self.assertIn(self.config.bookmarks[0].url, first)
        self.assertNotIn(self.config.bookmarks[1].url, first)
        self.assertIn('--restore-last-session', resumed)
        self.assertNotIn(self.config.bookmarks[0].url, resumed)
        self.assertEqual(blank[-1], 'about:blank')

    def test_live_bookmark_edit_preserves_runtime_and_profile(self):
        marker = self.store.safe('workspaces', 'demo', 'chrome-profile', 'marker')
        marker.write_text('keep')
        before = self.store.runtime('demo')
        changed = replace(self.config, bookmarks=(Bookmark('New', 'http://localhost:8888'),))
        with patch.object(self.browser, 'observe', return_value={'state': 'running'}) as observe:
            self.manager.update(changed)
            observe.assert_not_called()
            with self.assertRaisesRegex(Exception, 'Stop both'):
                self.manager.update(replace(changed, ssh_host='other'))
        self.assertEqual(self.store.workspace('demo').bookmarks, changed.bookmarks)
        self.assertEqual(self.store.runtime('demo'), before)
        self.assertEqual(marker.read_text(), 'keep')

    def test_one_or_all_selection_and_index_validation(self):
        with patch.object(self.browser, 'open_bookmarks') as notify:
            self.manager.open_bookmarks('demo', 1)
            notify.assert_called_once_with('demo', [self.config.bookmarks[1].url])
            self.manager.open_bookmarks('demo')
            self.assertEqual(notify.call_args.args, ('demo', [b.url for b in self.config.bookmarks]))
            for index in (-1, 2, True, '0'):
                with self.subTest(index=index), self.assertRaises(InvalidConfig):
                    self.manager.open_bookmarks('demo', index)

    def test_open_requires_existing_owned_browser_without_starting(self):
        with patch.object(self.browser, 'observe', return_value={'state': 'stopped'}), \
                patch('devhard.browser.subprocess.Popen') as spawn:
            with self.assertRaises(BrowserError) as error:
                self.manager.open_bookmarks('demo')
            self.assertEqual(error.exception.code, 'browser_not_running')
            spawn.assert_not_called()
        self.assertIsNone(self.store.runtime('demo').reserved_port)

    def owned_record(self):
        marker = '--devhard-instance=original'
        argv = ['/chrome', f'--user-data-dir={self.browser.profile("demo")}',
                '--proxy-server=socks5://127.0.0.1:60000', marker]
        record = {'pid': 123456, 'started': 'macos:1:2', 'executable': '/chrome', 'argv': argv, 'marker': marker}
        self.store.save_runtime('demo', RuntimeState(reserved_port=60000,
            processes={'tunnel': None, 'browser': record}, browser_initialized=True))
        return record

    def test_notify_keeps_original_record_and_does_not_restore_session(self):
        record = self.owned_record()
        before = self.store.runtime('demo')
        child = Mock()
        child.wait.return_value = 0
        with patch.object(self.browser, 'observe', return_value={'state': 'running'}), \
                patch('devhard.browser.process.identity', return_value='owned'), \
                patch('devhard.browser.process.find_launch', return_value=[]), \
                patch('devhard.browser.process.profile_processes', return_value=[record['pid']]), \
                patch('devhard.browser.subprocess.Popen', return_value=child) as spawn:
            result = self.manager.open_bookmarks('demo', 1)
        self.assertEqual(result['mode'], 'focus_or_open')
        self.assertEqual(result['outcomes'], [{'url': self.config.bookmarks[1].url, 'action': 'unconfirmed'}])
        spawn.assert_called_once()  # Missing results must never resend an already-opened URL.
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[-1], self.config.bookmarks[1].url)
        self.assertIn('--new-tab', argv)
        self.assertIn('--focus=' + self.config.bookmarks[1].url, argv)
        self.assertNotIn('--restore-last-session', argv)
        self.assertEqual(self.store.runtime('demo'), before)
        self.assertFalse(self.browser.journal_path('demo').exists())
        self.assertEqual(list(self.store.safe('workspaces', 'demo', 'runtime').glob('bookmark-focus-*.json')), [])

    def test_native_focus_report_and_no_match_are_successful_outcomes(self):
        record = self.owned_record()
        calls = []

        def spawn(argv, **kwargs):
            calls.append(argv)
            result_path = next(a.split('=', 1)[1] for a in argv if a.startswith('--focus-result-file='))
            child = Mock()

            def wait(**_):
                status = 'focused' if len(calls) == 1 else 'no_match'
                atomic_write(self.store.safe('workspaces', 'demo', 'runtime', result_path.rsplit('/', 1)[1]),
                             json_text({'status': status, 'exit_code': 0 if status == 'focused' else 1}))
                return 0
            child.wait.side_effect = wait
            return child

        with patch.object(self.browser, 'observe', return_value={'state': 'running'}), \
                patch('devhard.browser.process.identity', return_value='owned'), \
                patch('devhard.browser.process.find_launch', return_value=[]), \
                patch('devhard.browser.process.profile_processes', return_value=[record['pid']]), \
                patch('devhard.browser.subprocess.Popen', side_effect=spawn):
            result = self.manager.open_bookmarks('demo')
        self.assertEqual([r['action'] for r in result['outcomes']], ['focused', 'opened'])
        self.assertEqual([argv[-1] for argv in calls], [b.url for b in self.config.bookmarks])
        self.assertEqual(result['count'], 2)
        self.assertNotEqual([a for a in calls[0] if a.startswith('--focus-result-file=')],
                            [a for a in calls[1] if a.startswith('--focus-result-file=')])

    def test_selector_parse_error_retries_only_without_focus(self):
        with patch.object(self.browser, 'notify_bookmark', side_effect=['parse_error', 'opened_without_focus']) as notify:
            result = self.manager.open_bookmarks('demo', 0)
        self.assertEqual(notify.call_args_list[1].kwargs, {'use_focus': False})
        self.assertEqual(result['outcomes'][0]['action'], 'opened_without_focus')

    def test_literal_selector_syntax_does_not_focus_unrelated_tabs(self):
        record = self.owned_record()
        child = Mock()
        child.wait.return_value = 0
        urls = ['http://localhost/a,b', 'http://localhost/path*']
        with patch.object(self.browser, 'observe', return_value={'state': 'running'}), \
                patch('devhard.browser.process.identity', return_value='owned'), \
                patch('devhard.browser.process.find_launch', return_value=[]), \
                patch('devhard.browser.process.profile_processes', return_value=[record['pid']]), \
                patch('devhard.browser.subprocess.Popen', return_value=child) as spawn:
            result = self.browser.open_bookmarks('demo', urls)
        self.assertTrue(all(r['action'] == 'opened_without_focus' for r in result['outcomes']))
        for call, url in zip(spawn.call_args_list, urls):
            self.assertEqual(call.args[0][-1], url)
            self.assertFalse(any(a.startswith('--focus=') for a in call.args[0]))

    def test_interrupted_notification_cleans_only_its_own_new_process(self):
        original = self.owned_record()
        marker = '--devhard-instance=notifier'
        argv = [*original['argv'][:-1], marker, '--new-tab', self.config.bookmarks[0].url]
        raw = {'schema_version': 1, 'purpose': 'open_bookmarks', 'argv': argv, 'marker': marker}
        candidate = {**original, 'pid': 123457, 'argv': argv, 'marker': marker}
        path = self.browser.journal_path('demo')
        atomic_write(path, json_text(raw))
        with patch('devhard.browser.process.find_launch', return_value=[candidate]), \
                patch('devhard.browser.process.profile_processes', side_effect=[[candidate['pid']], []]), \
                patch.object(self.browser, 'terminate') as terminate:
            self.browser.reconcile('demo')
        terminate.assert_called_once_with(candidate)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.runtime('demo').processes['browser'], original)

    def test_interrupted_notification_never_clears_foreign_profile_owner(self):
        original = self.owned_record()
        marker = '--devhard-instance=notifier'
        raw = {'schema_version': 1, 'purpose': 'open_bookmarks',
               'argv': [*original['argv'][:-1], marker, '--new-tab', self.config.bookmarks[0].url], 'marker': marker}
        atomic_write(self.browser.journal_path('demo'), json_text(raw))
        with patch('devhard.browser.process.find_launch', return_value=[]), \
                patch('devhard.browser.process.profile_processes', return_value=[654321]), \
                patch('devhard.browser.process.identity', return_value='exited'), \
                patch.object(self.browser, 'terminate') as terminate:
            with self.assertRaises(BrowserError):
                self.browser.reconcile('demo')
        terminate.assert_not_called()
        self.assertTrue(self.browser.journal_path('demo').exists())
