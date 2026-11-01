"""Linux-only isolated tests: restoration on failure, and watchdog ordering."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tempfile

if sys.platform == 'win32':
    import pytest
    pytest.skip('Uses the same flock implementation as Linux target', allow_module_level=True)

spec = importlib.util.spec_from_file_location('service_drill', Path(__file__).parents[1]/'scripts/stage12-service-drill.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class RestorationSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.calls = []
        self.current = dict(sha='a'*40, image_id='test-image')
        drill.events = []
        drill.report = None
        p = patch.object(drill, 'record')
        p.start()
        self.addCleanup(p.stop)
        for name, value in [('STATE', root), ('DRILL_STATE', root/'reports')]:
            p = patch.object(drill, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(drill, 'preflight', return_value=(self.current, root/'compose.json'))
        p.start()
        self.addCleanup(p.stop)
        self.args = SimpleNamespace(kind='nginx', restore_sha='b'*40)

    def invoke(self, failure):
        def fake_command(argv, **kwargs):
            self.calls.append(argv)
            if failure(argv):
                raise RuntimeError('injected isolated command failure')
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(drill, 'command', side_effect=fake_command), patch.object(drill, 'snapshot', return_value={}), patch.object(drill, 'healthy', return_value=True):
            with self.assertRaises(RuntimeError):
                drill.execute(self.args)

    def test_stop_failure_still_requests_restore_before_timer_cancel(self):
        self.invoke(lambda a: a == ['systemctl', 'stop', 'nginx'])
        restore = ['/usr/bin/systemctl', 'start', 'nginx']
        self.assertIn(restore, self.calls)
        self.assertEqual(self.calls[0][0], 'systemd-run')
        self.assertEqual(self.calls[1][:3], ['systemctl', 'is-active', '--quiet'])
        self.assertFalse(any(a[0:2] == ['systemctl', 'stop'] and a[-1].endswith('.timer') for a in self.calls))

    def test_failed_watchdog_verification_prevents_stop(self):
        self.invoke(lambda a: a[:3] == ['systemctl', 'is-active', '--quiet'])
        self.assertNotIn(['systemctl', 'stop', 'nginx'], self.calls)

    def test_restore_failure_keeps_independent_timer(self):
        self.invoke(lambda a: a == ['/usr/bin/systemctl', 'start', 'nginx'])
        # Empty snapshot also ends the injected fault, ensuring finally is run.
        self.assertIn(['/usr/bin/systemctl', 'start', 'nginx'], self.calls)
        self.assertFalse(any(a[-1].endswith('.timer') and a[:2] == ['systemctl', 'stop'] for a in self.calls))

    def test_wall_deadline_interrupts_observation_then_restores(self):
        def fake_command(argv, **kwargs):
            self.calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(drill, 'command', side_effect=fake_command), patch.object(drill, 'snapshot', side_effect=[drill.FaultWindowEnded(), {}]), patch.object(drill, 'healthy', return_value=True):
            drill.execute(self.args)
        self.assertIn(['/usr/bin/systemctl', 'start', 'nginx'], self.calls)
        self.assertTrue(any(a[-1].endswith('.timer') and a[:2] == ['systemctl', 'stop'] for a in self.calls))


if __name__ == '__main__':
    unittest.main()
