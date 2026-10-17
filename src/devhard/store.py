"""Owned data tree and atomic versioned storage."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import uuid

from .config import DevHardError, GlobalConfig, InvalidConfig, RuntimeState, WorkspaceConfig, name


def atomic_write(path: Path, data: str):
    """Publish a complete file, preserving the previous file on pre-rename failure."""
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def workspace_toml(config: WorkspaceConfig):
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    return (f'schema_version = 1\nname = {quote(config.name)}\n'
            + ('bookmarks = []\n' if not config.bookmarks else '')
            + f'\n[ssh]\nhost = {quote(config.ssh_host)}\n'
            f'\n[browser]\nprofile = "chrome-profile"\nexecutable = {quote(config.executable)}\n'
            f'restore_session = {str(config.restore_session).lower()}\n'
            + ''.join('\n[[bookmarks]]\n' + ''.join(f'{key} = {str(value).lower() if type(value) is bool else quote(value)}\n'
                       for key, value in bookmark.as_dict().items()) for bookmark in config.bookmarks))


class Store:
    def __init__(self, root: Path | str | None = None):
        # Explicit root aliases resolve to one physical directory and hence one lock.
        self.root = Path(root if root is not None else '~/.devhard').expanduser().resolve()
        self._write_enabled = False

    def safe(self, *parts):
        path = self.root.joinpath(*parts)
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise InvalidConfig('path escapes data root') from None
        if '..' in relative.parts:
            raise InvalidConfig('path escapes data root')
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise InvalidConfig(f'symlink not allowed in managed data: {current}')
        return path

    def prepare_lock_directory(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.safe('runtime').mkdir(mode=0o700, exist_ok=True)

    def require_writer(self):
        if not self._write_enabled:
            raise DevHardError('storage mutation requires the Manager lock')

    def initialize(self):
        self.require_writer()
        self.safe('workspaces').mkdir(mode=0o700, exist_ok=True)
        path = self.safe('config.toml')
        if not path.exists():
            atomic_write(path, 'schema_version = 1\n\n[ports]\nmin = 60000\nmax = 60999\n')
        return self.global_config()

    def read(self, path, toml=False):
        try:
            content = path.read_text(encoding='utf-8')
            return tomllib.loads(content) if toml else json.loads(content)
        except (ValueError, UnicodeError) as error:
            raise InvalidConfig(f'{path}: {error}') from error

    def global_config(self):
        return GlobalConfig.parse(self.read(self.safe('config.toml'), toml=True))

    def workspace_path(self, workspace_name):
        return self.safe('workspaces', name(workspace_name))

    def workspace(self, workspace_name):
        config = WorkspaceConfig.parse(self.read(self.safe('workspaces', name(workspace_name), 'config.toml'), toml=True))
        if config.name != workspace_name:
            raise InvalidConfig('workspace name does not match directory')
        self.safe('workspaces', workspace_name, 'chrome-profile')
        return config

    def workspaces(self):
        parent = self.safe('workspaces')
        if not parent.exists():
            return []
        result = []
        for entry in sorted(parent.iterdir()):
            if entry.name.startswith('.create-'):
                continue  # An unpublished registration interrupted before directory rename.
            result.append(self.workspace(entry.name))
        return result

    def runtime(self, workspace_name):
        return RuntimeState.parse(self.read(self.safe('workspaces', name(workspace_name), 'runtime', 'state.json')))

    def save_runtime(self, workspace_name, state):
        self.require_writer()
        validated = RuntimeState.parse(state.as_dict())
        path = self.safe('workspaces', name(workspace_name), 'runtime', 'state.json')
        atomic_write(path, json_text(validated.as_dict()))

    def register(self, config):
        self.require_writer()
        config = WorkspaceConfig.parse(config.as_dict())
        destination = self.workspace_path(config.name)
        if destination.exists():
            raise DevHardError(f'workspace already exists: {config.name}')
        temporary = self.safe('workspaces', f'.create-{uuid.uuid4().hex}')
        temporary.mkdir(mode=0o700)
        try:
            (temporary / 'chrome-profile').mkdir(mode=0o700)
            (temporary / 'runtime').mkdir(mode=0o700)
            atomic_write(temporary / 'runtime' / 'state.json', json_text(RuntimeState().as_dict()))
            atomic_write(temporary / 'config.toml', workspace_toml(config))
            temporary.rename(destination)
            fsync_directory(destination.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return config
