"""Real Chrome bookmark opening; CDP is used only to observe disposable test tabs."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from browser_probe import cdp, targets, wait_for
from devhard.browser import BrowserError
from devhard.config import Bookmark, WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store, atomic_write, json_text
from devhard import process


def main():
    repo = Path(__file__).resolve().parents[1]
    fixture = json.loads((repo / '.fixture-runs/active.json').read_text())
    base = repo / '.m4-runs'
    root = Path(tempfile.mkdtemp(prefix='bookmarks-', dir=base))
    atomic_write(root / 'config.toml', 'schema_version = 1\n[ssh]\nconfig_file = '
                 + json.dumps(fixture['ssh_config']) + '\n')
    result = {'root': str(root), 'checks': [], 'cleanup_errors': []}
    config = WorkspaceConfig('bookmarks', fixture['servers'][0]['alias'], bookmarks=(
        Bookmark('App', 'http://127.0.0.1:3000/#initial', 'Main development app', True),
        Bookmark('API docs', 'http://127.0.0.1:6767/#docs', 'Open on demand')))

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        result['checks'].append(label)
        print('PASS ' + label, flush=True)

    with Manager(Store(root)) as manager:
        manager.browsers.extra_args = ('--remote-debugging-port=0',)
        profile = manager.browsers.profile(config.name)

        def pages(count):
            return wait_for(lambda: (entries if len(entries := targets(profile)) == count else None))

        try:
            manager.register(config)
            check('Start All', manager.workspace_action(config.name, 'start_all')['result'] == 'success')
            first = pages(1)
            check('first launch opens only Initial bookmark', first[0]['url'] == config.bookmarks[0].url)
            original = manager.store.runtime(config.name)
            check('single bookmark accepted', manager.open_bookmarks(config.name, 1)['count'] == 1)
            check('single bookmark adds a tab in original instance', Counter(p['url'] for p in pages(2)) == Counter(b.url for b in config.bookmarks))
            response = manager.open_bookmarks(config.name)
            check('Open all focuses both existing bookmarks without duplicates',
                  Counter(p['url'] for p in pages(2)) == Counter(b.url for b in config.bookmarks)
                  and all(r['action']=='focused' for r in response['outcomes']))
            response = manager.open_bookmarks(config.name, 0)
            entries = pages(2)
            check('repeated Open focuses instead of adding another tab', response['outcomes'][0]['action']=='focused')
            first_target = next(p for p in entries if p['url']==config.bookmarks[0].url)
            def first_visible():
                value = cdp(first_target['webSocketDebuggerUrl'], 'Runtime.evaluate', {
                    'expression': 'document.visibilityState', 'returnByValue': True})
                return value['result'].get('value')=='visible'
            check('previously inactive tab becomes visible', wait_for(first_visible))
            added = Bookmark('Metrics','http://127.0.0.1:6767/?view=metrics#panel')
            manager.update(replace(config,bookmarks=(*config.bookmarks,added)))
            response = manager.open_bookmarks(config.name)
            check('mixed Open all focuses old URLs and opens the missing URL',
                  len(pages(3))==3 and [r['action'] for r in response['outcomes']]==['focused','focused','opened'])
            manager.open_bookmarks(config.name)
            entries=pages(3)
            check('repeating mixed Open all leaves tab count unchanged',len(entries)==3)
            check('native browser record, tunnel and reservation unchanged', manager.store.runtime(config.name) == original)
            for entry in entries:
                def loaded():
                    value = cdp(entry['webSocketDebuggerUrl'], 'Runtime.evaluate', {
                        'expression': "document.documentElement.dataset.fixtureId || ''", 'returnByValue': True})
                    return value['result'].get('value')
                check('opened page reached expected remote fixture', wait_for(loaded) == fixture['servers'][0]['fixture_id'])
            changed = replace(config, bookmarks=(Bookmark('Renamed', config.bookmarks[0].url, 'Updated while running', True),))
            manager.update(changed)
            check('live bookmark edit preserves original browser', manager.store.runtime(config.name) == original)
            manager.open_bookmarks(config.name)
            check('Open all uses edited bookmark list without duplicates', len(pages(3)) == 3)
            special = [Bookmark('Comma','http://127.0.0.1:6767/a,b'),
                       Bookmark('Literal star','http://127.0.0.1:6767/path*'),
                       Bookmark('Unicode','http://127.0.0.1:6767/한글')]
            manager.update(replace(config,bookmarks=tuple(special)))
            check('comma URL opens literally without selector interpretation',
                  manager.open_bookmarks(config.name,0)['outcomes'][0]['action']=='opened_without_focus' and len(pages(4))==4)
            check('literal trailing star never becomes a prefix selector',
                  manager.open_bookmarks(config.name,1)['outcomes'][0]['action']=='opened_without_focus' and len(pages(5))==5)
            check('Unicode URL opens on no match',manager.open_bookmarks(config.name,2)['outcomes'][0]['action']=='opened' and len(pages(6))==6)
            check('Unicode URL focuses on repeated Open',manager.open_bookmarks(config.name,2)['outcomes'][0]['action']=='focused' and len(pages(6))==6)
            manager.workspace_action(config.name, 'stop_all')
            try:
                manager.open_bookmarks(config.name)
                raise AssertionError('Open unexpectedly started Chrome')
            except BrowserError as error:
                check('stopped Chrome is never started by Open', error.code == 'browser_not_running')
            manager.update(replace(changed, restore_session=False))
            manager.workspace_action(config.name, 'start_all')
            check('later launch without restoration opens blank, not Initial', pages(1)[0]['url'] == 'about:blank')

            # Force the precise check→spawn race: the owned browser exits just before
            # the notifier starts. A replacement must be cleaned, never adopted.
            record = manager.store.runtime(config.name).processes['browser']
            real_popen = subprocess.Popen
            triggered = False

            def raced(argv, *args, **kwargs):
                nonlocal triggered
                if '--new-tab' in argv and not triggered:
                    triggered = True
                    manager.browsers.terminate(record)
                return real_popen(argv, *args, **kwargs)

            with patch('devhard.browser.subprocess.Popen', side_effect=raced):
                try:
                    manager.open_bookmarks(config.name)
                    raise AssertionError('exit race silently started a replacement browser')
                except BrowserError:
                    pass
            check('exit race was exercised', triggered)
            check('exit race leaves no unexpected browser', process.profile_processes(profile) == [])
            check('exit race leaves no notification journal', not manager.browsers.journal_path(config.name).exists())
            check('exit race does not adopt a replacement', manager.store.runtime(config.name).processes['browser'] == record)
            result['success'] = True
        finally:
            try:
                manager.workspace_action(config.name, 'stop_all')
            except Exception as error:
                result['cleanup_errors'].append(str(error))
            atomic_write(root / 'result.json', json_text(result))
            print('Evidence: ' + str(root / 'result.json'))
    if result['cleanup_errors']:
        raise RuntimeError(result['cleanup_errors'])


if __name__ == '__main__':
    main()
