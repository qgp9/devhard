from concurrent.futures import ThreadPoolExecutor
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from devhard.api import create_app
from devhard.config import WorkspaceConfig
from devhard.manager import Manager
from devhard.store import Store
from devhard.tunnel import TunnelError


ORIGIN = 'http://127.0.0.1:8123'


class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = Manager(Store(temporary.name)).__enter__()
        self.addCleanup(self.manager.__exit__, None, None, None)
        self.manager.api_url = ORIGIN
        self.manager.publish('ready')
        self.app = create_app(self.manager, ORIGIN)
        self.client = TestClient(self.app, base_url=ORIGIN).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.page = self.client.get('/')
        token = re.search(r'name="devhard-token" content="([^"]+)"', self.page.text)[1]
        self.headers = {'Authorization': 'Bearer ' + token, 'Origin': ORIGIN}

    def request(self, path, method='GET', body=None, headers=None):
        return self.client.request(method, path, json=body,
                                   headers=self.headers if headers is None else headers)

    def wait(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.request('/api/jobs/' + job['id']).json()
            if result['state'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('job did not finish')

    def register(self, name='demo'):
        return self.request('/api/workspaces', 'POST', WorkspaceConfig(name, 'dev1.h').as_dict())

    def test_bootstrap_security_and_rebinding_csrf_rejection(self):
        self.assertEqual(self.page.status_code, 200)
        self.assertEqual(self.page.headers['cache-control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", self.page.headers['content-security-policy'])
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        for path in ('/', '/api/state', '/assets/app.js'):
            self.assertEqual(self.request(path, headers={**self.headers, 'Host': 'attacker.test:8123'}).status_code, 403)
            self.assertEqual(self.request(path, headers={**self.headers, 'Origin': 'https://attacker.test'}).status_code, 403)
        for origin in ('null', 'http://localhost:8123', 'http://127.0.0.1:8124'):
            self.assertEqual(self.request('/api/state', headers={**self.headers, 'Origin': origin}).status_code, 403)
        no_origin = {'Authorization': self.headers['Authorization']}
        self.assertEqual(self.request('/api/workspaces', 'POST', {}, headers=no_origin).status_code, 403)
        self.assertEqual(self.request('/api/state', headers=no_origin).status_code, 200)
        self.assertNotIn('access-control-allow-origin', self.page.headers)
        self.assertEqual(self.request('/api/state', headers={'Authorization': 'Bearer wrong'}).status_code, 401)

    def test_no_get_side_effect_and_no_static_traversal(self):
        self.assertEqual(self.wait(self.register().json())['state'], 'succeeded')
        root = self.manager.store.root
        files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
        for _ in range(2):
            self.assertEqual(self.request('/api/state').status_code, 200)
        self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertEqual(self.client.get('/assets/config.toml').status_code, 404)
        self.assertEqual(self.client.get('/openapi.json').status_code, 404)
        self.assertEqual(self.client.get('/assets/app.js').status_code, 200)

    def test_two_clients_duplicate_registration_and_edit_preserves_profile(self):
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: self.register(), range(2)))
        self.assertEqual([r.status_code for r in responses], [202, 202])
        outcomes = [self.wait(r.json())['state'] for r in responses]
        self.assertEqual(sorted(outcomes), ['failed', 'succeeded'])
        marker = self.manager.store.safe('workspaces', 'demo', 'chrome-profile', 'marker')
        marker.write_text('preserved')
        config = WorkspaceConfig('demo', 'ops.h', ('http://127.0.0.1:6767',), restore_session=False)
        updated = self.request('/api/workspaces/demo', 'PUT', config.as_dict())
        self.assertEqual(self.wait(updated.json())['state'], 'succeeded')
        self.assertEqual(self.manager.store.workspace('demo'), config)
        self.assertEqual(marker.read_text(), 'preserved')
        with patch.object(self.manager.browsers, 'observe', return_value={'state': 'unknown'}):
            refused = self.request('/api/workspaces/demo', 'PUT', WorkspaceConfig('demo', 'other').as_dict())
            self.assertEqual(self.wait(refused.json())['state'], 'failed')
        self.assertEqual(self.manager.store.workspace('demo'), config)

    def test_accepted_running_queued_and_nonblocking_poll(self):
        self.wait(self.register().json())
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        order = []

        def slow(name, action):
            order.append(action)
            if action == 'start':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release')
            return {'state': 'stopped'}

        with patch.object(self.manager, 'tunnel_action', side_effect=slow):
            first = self.request('/api/workspaces/demo/tunnel/start', 'POST', {})
            self.assertEqual(first.status_code, 202)
            self.assertTrue(entered.wait(2))
            second = self.request('/api/workspaces/demo/tunnel/stop', 'POST', {})
            started = time.monotonic()
            state = self.request('/api/state').json()
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(self.request(first.headers['location']).json()['state'], 'running')
            self.assertEqual(self.request(second.headers['location']).json()['state'], 'queued')
            self.assertEqual(len(state['workspaces']), 1)
            release.set()
            self.assertEqual(self.wait(first.json())['state'], 'succeeded')
            self.assertEqual(self.wait(second.json())['state'], 'succeeded')
        self.assertEqual(order, ['start', 'stop'])

    def test_partial_and_failure_are_finished_job_outcomes(self):
        self.wait(self.register().json())
        with patch.object(self.manager.tunnels, 'start', return_value={}), \
                patch.object(self.manager.browsers, 'start', side_effect=TunnelError('browser_missing', 'not installed')):
            result = self.wait(self.request('/api/workspaces/demo/workspace/start_all', 'POST', {}).json())
            self.assertEqual(result['state'], 'partial')
            self.assertEqual(result['error']['code'], 'browser_missing')
        with patch.object(self.manager, 'tunnel_action', side_effect=TunnelError('authentication_failed', 'denied')):
            result = self.wait(self.request('/api/workspaces/demo/tunnel/start', 'POST', {}).json())
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['error']['code'], 'authentication_failed')

    def test_invalid_and_oversized_requests_rejected_before_submission(self):
        for config in ({}, WorkspaceConfig('../escape', 'host').as_dict(),
                       WorkspaceConfig('demo', '-oCommand=whoami').as_dict()):
            self.assertEqual(self.request('/api/workspaces', 'POST', config).status_code, 400)
        for content, status in (('{bad', 400), ('x' * 16385, 413)):
            response = self.client.post('/api/workspaces', content=content,
                headers={**self.headers, 'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.client.post('/api/workspaces', content='hello', headers=self.headers).status_code, 415)
        self.assertEqual(self.request('/api/workspaces/demo/browser/erase', 'POST', {}).status_code, 404)
        self.assertEqual(self.request('/api/state').json()['jobs'], [])

    def test_token_rotates_and_jobs_are_scoped_to_manager_run(self):
        with TestClient(create_app(self.manager, ORIGIN), base_url=ORIGIN) as other:
            self.assertEqual(other.get('/api/state', headers=self.headers).status_code, 401)

    def test_bookmarks_open_is_an_authenticated_queued_command(self):
        self.wait(self.register().json())
        with patch.object(self.manager, 'open_bookmarks', return_value={'result': 'submitted', 'count': 1}) as opened:
            job = self.request('/api/workspaces/demo/bookmarks/open', 'POST', {'index': 0})
            self.assertEqual(job.status_code, 202)
            self.assertEqual(self.wait(job.json())['state'], 'succeeded')
            opened.assert_called_once_with('demo', 0)
        for body in ({'index': True}, {'index': -1}, {'url': 'http://arbitrary.test'}):
            self.assertEqual(self.request('/api/workspaces/demo/bookmarks/open', 'POST', body).status_code, 400)

    def test_stop_all_workspaces_stops_every_workspace(self):
        self.wait(self.register('a').json())
        self.wait(self.register('b').json())
        with patch.object(self.manager.browsers, 'stop') as browser_stop, \
                patch.object(self.manager.tunnels, 'stop_all') as tunnel_stop:
            job = self.request('/api/workspaces/stop_all', 'POST', {})
            self.assertEqual(job.status_code, 202)
            self.assertEqual(self.wait(job.json())['state'], 'succeeded')
        self.assertEqual([entry.args[0] for entry in browser_stop.call_args_list], ['a', 'b'])
        self.assertEqual([entry.args[0] for entry in tunnel_stop.call_args_list], ['a', 'b'])

    def test_stop_all_workspaces_reports_partial_failures(self):
        self.wait(self.register('a').json())
        self.wait(self.register('b').json())
        with patch.object(self.manager.browsers, 'stop',
                          side_effect=[None, TunnelError('browser_ownership_unknown', 'unverified')]), \
                patch.object(self.manager.tunnels, 'stop_all') as tunnel_stop:
            result = self.wait(self.request('/api/workspaces/stop_all', 'POST', {}).json())
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['error']['code'], 'stop_all_partial')
        self.assertEqual([entry['workspace'] for entry in result['result']['errors']], ['b'])
        self.assertEqual(result['result']['errors'][0]['code'], 'browser_ownership_unknown')
        tunnel_stop.assert_called_once_with('a')

    def test_command_queue_is_bounded(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(*_):
            entered.set()
            release.wait(5)
            return {}

        with patch.object(self.manager, 'tunnel_action', side_effect=slow):
            first = self.request('/api/workspaces/demo/tunnel/start', 'POST', {})
            self.assertTrue(entered.wait(2))
            for _ in range(31):
                self.assertEqual(self.request('/api/workspaces/demo/tunnel/start', 'POST', {}).status_code, 202)
            self.assertEqual(self.request('/api/workspaces/demo/tunnel/start', 'POST', {}).status_code, 429)
            release.set()
            self.wait(first.json())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(j['state'] in ('queued', 'running')
                    for j in self.request('/api/state').json()['jobs']):
                time.sleep(.01)
            self.assertTrue(all(j['state'] == 'succeeded' for j in self.request('/api/state').json()['jobs']))

