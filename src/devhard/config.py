"""Versioned configuration and runtime schemas; no filesystem side effects."""

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit


class DevHardError(Exception):
    """Expected user-visible failure."""


class InvalidConfig(DevHardError):
    pass


def fields(value, allowed, required=()):
    if not isinstance(value, dict):
        raise InvalidConfig('expected a table/object')
    unknown = value.keys() - set(allowed)
    missing = set(required) - value.keys()
    if unknown or missing:
        raise InvalidConfig(f'unknown fields: {sorted(unknown)}; missing fields: {sorted(missing)}')


def version(value):
    if type(value) is not int or value != 1:
        raise InvalidConfig(f'unsupported schema_version: {value!r}')


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise InvalidConfig(f'{label} must be an integer in {low}..{high}')


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', value):
        raise InvalidConfig('name must be 1..63 lowercase letters, digits, underscores or hyphens; start with a letter or digit')
    return value


@dataclass(frozen=True)
class GlobalConfig:
    schema_version: int = 1
    port_min: int = 60000
    port_max: int = 60999

    @classmethod
    def parse(cls, raw):
        fields(raw, ['schema_version', 'ports'], ['schema_version'])
        version(raw['schema_version'])
        ports = raw.get('ports', {})
        fields(ports, ['min', 'max'])
        low, high = ports.get('min', 60000), ports.get('max', 60999)
        integer(low, 1024, 65535, 'ports.min')
        integer(high, low, 65535, 'ports.max')
        return cls(port_min=low, port_max=high)


@dataclass(frozen=True)
class WorkspaceConfig:
    name: str
    ssh_host: str
    tabs: tuple[str, ...] = ()
    executable: str = 'auto'
    restore_session: bool = True
    schema_version: int = 1

    @classmethod
    def parse(cls, raw):
        fields(raw, ['schema_version', 'name', 'ssh', 'browser', 'tabs'],
               ['schema_version', 'name', 'ssh'])
        version(raw['schema_version'])
        workspace_name = name(raw['name'])
        ssh = raw['ssh']
        fields(ssh, ['host'], ['host'])
        host = ssh['host']
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_@.:[\]-]+', host) or host.startswith('-'):
            raise InvalidConfig('ssh.host must be a host/SSH alias without options or whitespace')
        browser = raw.get('browser', {})
        fields(browser, ['profile', 'executable', 'restore_session'])
        if browser.get('profile', 'chrome-profile') != 'chrome-profile':
            raise InvalidConfig('browser.profile must be chrome-profile inside the workspace')
        executable = browser.get('executable', 'auto')
        if not isinstance(executable, str) or not executable or any(c in executable for c in '\0\r\n'):
            raise InvalidConfig('browser.executable must be a nonempty executable path or auto')
        restore = browser.get('restore_session', True)
        if type(restore) is not bool:
            raise InvalidConfig('browser.restore_session must be a boolean')
        tabs = raw.get('tabs', [])
        if not isinstance(tabs, list):
            raise InvalidConfig('tabs must be an array of HTTP(S) URLs')
        for url in tabs:
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                if (not parsed or parsed.scheme not in ('http', 'https') or not parsed.hostname
                        or any(c.isspace() or ord(c) < 32 for c in url) or parsed.port == 0):
                    raise ValueError()
            except ValueError:
                raise InvalidConfig('tabs must contain valid HTTP(S) URLs') from None
        return cls(workspace_name, host, tuple(tabs), executable, restore)

    def as_dict(self):
        return {'schema_version': self.schema_version, 'name': self.name,
                'ssh': {'host': self.ssh_host},
                'browser': {'profile': 'chrome-profile', 'executable': self.executable,
                            'restore_session': self.restore_session}, 'tabs': list(self.tabs)}


@dataclass(frozen=True)
class RuntimeState:
    reserved_port: int | None = None
    processes: dict = field(default_factory=lambda: {'tunnel': None, 'browser': None})
    schema_version: int = 1
    last_error: dict | None = None
    browser_initialized: bool = False

    @classmethod
    def parse(cls, raw):
        fields(raw, ['schema_version', 'reserved_port', 'processes', 'last_error', 'browser_initialized'],
               ['schema_version', 'reserved_port', 'processes'])
        version(raw['schema_version'])
        port = raw['reserved_port']
        if port is not None:
            integer(port, 1, 65535, 'reserved_port')
        processes = raw['processes']
        fields(processes, ['tunnel', 'browser'], ['tunnel', 'browser'])
        for record in processes.values():
            if record is None:
                continue
            fields(record, ['pid', 'started', 'executable', 'argv', 'marker'],
                   ['pid', 'started', 'executable', 'argv', 'marker'])
            integer(record['pid'], 1, 2**31 - 1, 'pid')
            for key in ('started', 'executable', 'marker'):
                if not isinstance(record[key], str) or not record[key]:
                    raise InvalidConfig(f'process {key} must be a nonempty string')
            if not isinstance(record['argv'], list) or not all(isinstance(v, str) for v in record['argv']):
                raise InvalidConfig('process argv must be a string array')
        if any(record is not None for record in processes.values()) and port is None:
            raise InvalidConfig('process records require a reserved_port')
        error = raw.get('last_error')
        if error is not None:
            fields(error, ['code', 'message', 'at'], ['code', 'message', 'at'])
            if not all(isinstance(value, str) and value for value in error.values()):
                raise InvalidConfig('last_error fields must be nonempty strings')
        initialized = raw.get('browser_initialized', False)
        if type(initialized) is not bool:
            raise InvalidConfig('browser_initialized must be a boolean')
        return cls(port, processes, last_error=error, browser_initialized=initialized)

    def as_dict(self):
        return {'schema_version': self.schema_version, 'reserved_port': self.reserved_port,
                'processes': self.processes, 'last_error': self.last_error,
                'browser_initialized': self.browser_initialized}
