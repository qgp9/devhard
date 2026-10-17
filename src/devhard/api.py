"""Local HTTP adapter: authenticated commands, serialized jobs, cached observations."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import asyncio
import copy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import secrets
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .config import DevHardError, InvalidConfig, WorkspaceConfig

WEB = Path(__file__).with_name('web')
LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


class Busy(DevHardError):
    pass


class ControlService:
    """One command worker; HTTP reads never wait for a process operation."""

    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.subscribers = {}
        self.streams_closed = False
        self.jobs = {}
        self.snapshot = {'workspaces': [], 'observed_at': None}
        self.accepting = True
        self.stopped = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='devhard-command')
        self.refresh()
        self.monitor = threading.Thread(target=self.observe, name='devhard-observe', daemon=True)
        self.monitor.start()

    def refresh(self):
        if not self.manager.operations.acquire(blocking=False):
            return
        try:
            workspaces = []
            for config in self.manager.store.workspaces():
                try:
                    workspaces.append(self.manager.view(config, self.manager.store.runtime(config.name)))
                except (DevHardError, OSError) as error:
                    workspaces.append({'name': config.name, 'config': config.as_dict(),
                                       'status': {'browser': 'unknown', 'tunnel': 'unknown'},
                                       'observation_error': str(error)})
            value = {'workspaces': workspaces, 'observed_at': now(), 'observation_error': None}
        except (DevHardError, OSError) as error:
            with self.lock:
                value = {**self.snapshot, 'observation_error': str(error)}
        finally:
            self.manager.operations.release()
        with self.lock:
            changed = (value['workspaces'] != self.snapshot['workspaces']
                       or value.get('observation_error') != self.snapshot.get('observation_error'))
            self.snapshot = value
            if changed:
                self.notify()

    def observe(self):
        while not self.stopped.wait(1):
            self.refresh()

    def state(self):
        with self.lock:
            return copy.deepcopy({**self.snapshot, 'manager': self.manager.info,
                                  'jobs': list(self.jobs.values())})

    @staticmethod
    def offer(queue, value):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(value)

    def notify(self):
        """Called under lock; one pending snapshot per client, never an event backlog."""
        if self.streams_closed:
            return
        value = self.state()
        for queue, loop in self.subscribers.items():
            loop.call_soon_threadsafe(self.offer, queue, value)

    def close_streams(self):
        with self.lock:
            self.accepting = False
            self.streams_closed = True
            for queue, loop in self.subscribers.items():
                loop.call_soon_threadsafe(self.offer, queue, None)

    async def events(self):
        queue = asyncio.Queue(maxsize=1)
        with self.lock:
            if self.streams_closed:
                return
            self.subscribers[queue] = asyncio.get_running_loop()
            queue.put_nowait(self.state())
        try:
            while True:
                try:
                    value = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ': heartbeat\n\n'
                    continue
                if value is None:
                    return
                yield 'event: state\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
        finally:
            with self.lock:
                self.subscribers.pop(queue, None)

    def submit(self, name, action, work):
        with self.lock:
            if not self.accepting:
                raise Busy('Manager is shutting down')
            if sum(j['state'] in ('queued', 'running') for j in self.jobs.values()) >= 32:
                raise Busy('Command queue is full; wait for a running command')
            # Retain recent outcomes, never evict a queued or running command.
            finished = [key for key, job in self.jobs.items() if job['state'] not in ('queued', 'running')]
            for key in finished[:max(0, len(self.jobs) - 99)]:
                del self.jobs[key]
            job = {'id': uuid.uuid4().hex, 'workspace': name, 'action': action,
                   'state': 'queued', 'accepted_at': now(), 'started_at': None,
                   'finished_at': None, 'error': None, 'result': None}
            self.jobs[job['id']] = job
            self.notify()
            self.executor.submit(self.execute, job['id'], work)
            return copy.deepcopy(job)

    def execute(self, job_id, work):
        with self.lock:
            job = self.jobs[job_id]
            job.update(state='running', started_at=now())
            self.notify()
        try:
            with self.manager.operations:
                result = work()
                if isinstance(result, WorkspaceConfig):
                    result = result.as_dict()
            outcome = 'partial' if isinstance(result, dict) and result.get('result') == 'partial' else 'succeeded'
            error = result.get('error') if isinstance(result, dict) else None
        except Exception as exception:
            if not isinstance(exception, (DevHardError, OSError)):
                LOG.exception('Command failed')
            result, outcome = None, 'failed'
            error = {'code': getattr(exception, 'code', 'command_failed'), 'message': str(exception)}
        self.refresh()
        with self.lock:
            job.update(state=outcome, result=result, error=error, finished_at=now())
            self.notify()

    def close(self):
        self.close_streams()
        with self.lock:
            self.accepting = False
        self.stopped.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.monitor.join()
        with self.lock:
            for job in self.jobs.values():
                if job['state'] == 'queued':
                    job.update(state='cancelled', finished_at=now())


async def json_body(request):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Use application/json')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise HTTPException(413, 'Request body exceeds 16 KiB')
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid JSON') from None


def create_app(manager, origin, on_ready=None):
    authority = origin.removeprefix('http://')
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        service = ControlService(manager)
        app.state.control = service
        try:
            if on_ready:
                on_ready()
            yield
        finally:
            # Blocking process cleanup stays off the ASGI event loop.
            import asyncio
            await asyncio.to_thread(service.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def local_access(request, call_next):
        hosts = request.headers.getlist('host')
        origins = request.headers.getlist('origin')
        if hosts != [authority]:
            response = JSONResponse({'detail': 'Invalid Host'}, status_code=403)
        elif origins and origins != [origin]:
            response = JSONResponse({'detail': 'Invalid Origin'}, status_code=403)
        elif request.url.path.startswith('/api/') and not secrets.compare_digest(
                request.headers.get('authorization', '').encode(), f'Bearer {token}'.encode()):
            response = JSONResponse({'detail': 'Missing or invalid control token'}, status_code=401)
        elif request.method not in ('GET', 'HEAD') and origins != [origin]:
            response = JSONResponse({'detail': 'Same-origin Origin required'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(DevHardError)
    async def config_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=429 if isinstance(error, Busy) else 400)

    @app.get('/', response_class=HTMLResponse)
    async def index():
        # Same-origin HTML bootstrap. No token in URLs, logs, cookies or runtime files.
        return HTMLResponse((WEB / 'index.html').read_text().replace('__CONTROL_TOKEN__', token))

    @app.get('/assets/{filename}')
    async def asset(filename: str):
        types = {'app.js': 'text/javascript', 'i18n.js': 'text/javascript', 'theme.js': 'text/javascript', 'style.css': 'text/css'}
        if filename not in types:
            raise HTTPException(404, 'Not found')
        return Response((WEB / filename).read_text(), media_type=types[filename])

    @app.get('/api/state')
    async def state():
        return app.state.control.state()

    @app.get('/api/events')
    async def events():
        return StreamingResponse(app.state.control.events(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/jobs/{job_id}')
    async def job(job_id: str):
        with app.state.control.lock:
            item = app.state.control.jobs.get(job_id)
            if item is None:
                raise HTTPException(404, 'Job not found (history is per Manager run)')
            return copy.deepcopy(item)

    def accepted(name, action, work):
        value = app.state.control.submit(name, action, work)
        return JSONResponse(value, status_code=202, headers={'Location': f'/api/jobs/{value["id"]}'})

    @app.post('/api/workspaces')
    async def register(request: Request):
        config = WorkspaceConfig.parse(await json_body(request))
        return accepted(config.name, 'register', lambda: manager.register(config))

    @app.post('/api/workspaces/stop_all')
    async def stop_all(request: Request):
        body = await json_body(request)
        if body != {}:
            raise InvalidConfig('Command body must be an empty object')
        return accepted('*', 'workspaces.stop_all', manager.stop_all_workspaces)

    @app.put('/api/workspaces/{name}')
    async def update(name: str, request: Request):
        config = WorkspaceConfig.parse(await json_body(request))
        if config.name != name:
            raise InvalidConfig('Workspace name cannot be changed')
        return accepted(name, 'update', lambda: manager.update(config))

    @app.post('/api/workspaces/{name}/{component}/{action}')
    async def command(name: str, component: str, action: str, request: Request):
        from .config import name as validate_name
        validate_name(name)
        if component == 'bookmarks' and action == 'open':
            from .config import fields
            body = await json_body(request)
            fields(body, ['index'])
            if 'index' in body and (type(body['index']) is not int or body['index'] < 0):
                raise InvalidConfig('bookmark index must be a nonnegative integer')
            return accepted(name, 'bookmarks.open', lambda: manager.open_bookmarks(name, body.get('index')))
        choices = {'workspace': ('start_all', 'stop_all'),
                   'browser': ('start', 'stop', 'restart'), 'tunnel': ('start', 'stop', 'restart')}
        if component not in choices or action not in choices[component]:
            raise HTTPException(404, 'Unknown command')
        body = await json_body(request)
        if body != {}:
            raise InvalidConfig('Command body must be an empty object')
        return accepted(name, f'{component}.{action}',
                        lambda: getattr(manager, component + '_action')(name, action))

    return app
