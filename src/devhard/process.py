"""Process identity and listener ownership on macOS/Linux. Never infer ownership from PID alone."""

import ctypes
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys


class InspectionError(Exception):
    pass


class BsdInfo(ctypes.Structure):
    # Apple sys/proc_info.h, struct proc_bsdinfo (PROC_PIDTBSDINFO = 3).
    _fields_ = [(key, ctypes.c_uint32) for key in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(key, ctypes.c_uint32) for key in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


def snapshot(pid):
    try:
        if sys.platform == 'darwin':
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            info = BsdInfo()
            size = lib.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info))
            if size == 0 and ctypes.get_errno() == errno.ESRCH:
                return None
            if size != ctypes.sizeof(info):
                raise InspectionError(f'cannot read process identity for PID {pid}')
            if info.status == 5:  # SZOMB
                return None
            buffer = ctypes.create_string_buffer(4096)
            if lib.proc_pidpath(pid, buffer, len(buffer)) <= 0:
                raise InspectionError(f'cannot read executable for PID {pid}')
            executable = os.fsdecode(buffer.value)
            started = f'macos:{info.start_sec}:{info.start_usec}'
        elif sys.platform.startswith('linux'):
            base = Path('/proc') / str(pid)
            stat = (base / 'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z':
                return None
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            started = f'linux:{boot}:{stat[19]}'
            executable = os.readlink(base / 'exe')
        else:
            raise InspectionError(f'unsupported process inspection platform: {sys.platform}')
        result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='],
                                capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode:
            # Disappearance is safe to report as absent, inspection denial is not.
            if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
                return None
            raise InspectionError(result.stderr.strip() or 'ps failed')
        return {'pid': pid, 'started': started, 'executable': executable,
                'command': result.stdout.strip()}
    except FileNotFoundError:
        if sys.platform.startswith('linux') and not Path(f'/proc/{pid}').exists():
            return None
        raise InspectionError('required process inspection tool/file is unavailable') from None
    except OSError as error:
        raise InspectionError(str(error)) from error


def matches(record, current):
    return (current is not None and record['started'] == current['started']
            and record['executable'] == current['executable']
            and ' '.join(record['argv']) == current['command']
            and f'SetEnv={record["marker"]}' in record['argv'])


def identity(record):
    if not record['started'].startswith(('macos:', 'linux:')):
        return 'unknown'  # Preserve M1/unsupported records without treating them as owned.
    current = snapshot(record['pid'])
    if current is None:
        return 'exited'
    return 'owned' if matches(record, current) else 'unknown'


def capture(pid, argv, marker):
    current = snapshot(pid)
    if (current is None or current['executable'] != argv[0]
            or current['command'] != ' '.join(argv) or f'SetEnv={marker}' not in argv):
        raise InspectionError('new SSH process identity could not be verified')
    return {key: current[key] for key in ('pid', 'started', 'executable')} | {'argv': argv, 'marker': marker}


def find_launch(argv, marker):
    result = subprocess.run(['ps', '-axww', '-o', 'pid=', '-o', 'command='],
                            capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise InspectionError(result.stderr.strip() or 'process discovery failed')
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == ' '.join(argv) and f'SetEnv={marker}' in argv:
            found.append(capture(int(parts[0]), argv, marker))
    return found


def owns_listener(pid, port):
    executable = shutil.which('lsof')
    if not executable:
        raise InspectionError('lsof is required to verify the SSH listening socket')
    result = subprocess.run([executable, '-nP', '-a', '-p', str(pid),
                             f'-iTCP@127.0.0.1:{port}', '-sTCP:LISTEN', '-Fn'],
                            capture_output=True, text=True, timeout=3,
                            env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode not in (0, 1) or (result.returncode and result.stderr.strip()):
        raise InspectionError(result.stderr.strip() or 'listener inspection failed')
    return f'n127.0.0.1:{port}' in result.stdout.splitlines()
