"""Safety regressions for the M0 experiment; actual lifecycle checks use the probe."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock


source = Path(__file__).resolve().parents[1] / 'experiments' / 'lifecycle_probe.py'
spec = importlib.util.spec_from_file_location('probe', source)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.state = {'pid': 100, 'started': 'Sat Oct 17 12:00:00 2026',
                      'command': 'chrome --user-data-dir=/tmp/unique-profile'}
        self.record = {**self.state, 'marker': '/tmp/unique-profile', 'name': 'chrome'}

    def test_reused_pid_cannot_be_stopped(self):
        different = {**self.state, 'started': 'Sat Oct 17 12:01:00 2026'}
        with patch.object(probe, 'snapshot', return_value=different), patch.object(probe.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'unverified'):
                probe.terminate_owned(self.record)
            kill.assert_not_called()

    def test_changed_command_cannot_be_stopped(self):
        different = {**self.state, 'command': 'chrome --user-data-dir=/tmp/user-profile'}
        with patch.object(probe, 'snapshot', return_value=different), patch.object(probe.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'unverified'):
                probe.terminate_owned(self.record)
            kill.assert_not_called()

    def test_missing_marker_fails_ownership(self):
        with patch.object(probe, 'snapshot', return_value=self.state):
            self.assertFalse(probe.owned({**self.record, 'marker': '/tmp/other-profile'}))

    def test_wrong_process_group_cannot_be_stopped(self):
        with patch.object(probe, 'snapshot', return_value=self.state), \
             patch.object(probe.os, 'getpgid', return_value=99), \
             patch.object(probe.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'process group'):
                probe.terminate_owned(self.record)
            kill.assert_not_called()

    def test_already_exited_process_is_not_signalled(self):
        with patch.object(probe, 'snapshot', return_value=None), patch.object(probe.os, 'killpg') as kill:
            probe.terminate_owned(self.record)
            kill.assert_not_called()

    def test_inspection_error_does_not_leave_unrecorded_child(self):
        child = Mock(pid=100)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(probe.subprocess, 'Popen', return_value=child), \
             patch.object(probe, 'snapshot', side_effect=PermissionError('ps denied')):
            with self.assertRaises(PermissionError):
                probe.spawn_detached(Path(directory), 'worker', ['worker'], 'marker')
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_zombie_is_not_running(self):
        result = subprocess.CompletedProcess([], 0,
                    stdout='Sat Oct 17 12:00:00 2026 Z chrome --user-data-dir=/tmp/unique-profile\n')
        with patch.object(probe.subprocess, 'run', return_value=result):
            self.assertIsNone(probe.snapshot(100))


if __name__ == '__main__':
    unittest.main()
