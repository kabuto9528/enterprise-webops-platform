"""Isolated tests of bounded bad activation; no real release/service commands."""
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

if sys.platform == 'win32':
    import pytest
    pytest.skip('Tests Linux SIGALRM used on the deployment target', allow_module_level=True)

spec = importlib.util.spec_from_file_location('bad_drill', Path(__file__).parents[1]/'scripts/stage12-bad-release-drill.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class BoundedActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current = dict(sha=drill.GOOD, image_id='test-good-image', compose='/tmp/test-compose.json')
        root = Path(self.tmp.name)
        (root/'current.json').write_text(json.dumps(self.current))
        for name, value in [('STATE', root), ('record', Mock()), ('timer', None), ('activation_seen', False)]:
            p = patch.object(drill, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_recovery_uses_original_without_hold(self):
        original = Mock(return_value='healthy')
        with patch.object(drill, 'command') as command:
            self.assertEqual(drill.activation(self.current, original, dict(sha=drill.GOOD)), 'healthy')
        command.assert_not_called()

    def test_timer_verification_failure_prevents_activation(self):
        original = Mock()
        with patch.object(drill, 'command', side_effect=['', RuntimeError('inactive timer')]):
            with self.assertRaises(RuntimeError):
                drill.activation(self.current, original, dict(sha=drill.BAD))
        original.assert_not_called()

    def test_failed_bad_activation_is_observed_then_original_error_rethrown(self):
        failure = ValueError('original activation failed')
        original = Mock(side_effect=failure)
        with patch.object(drill, 'command'), patch.object(drill, 'bad_state', return_value={}), patch.object(drill.time, 'sleep', side_effect=drill.ObservationDeadline()):
            with self.assertRaises(ValueError) as caught:
                drill.activation(self.current, original, dict(sha=drill.BAD))
        self.assertIs(caught.exception, failure)

    def test_deadline_during_original_activation_does_not_enter_hold(self):
        original = Mock(side_effect=drill.ObservationDeadline())
        with patch.object(drill, 'command'), patch.object(drill, 'bad_state') as state:
            with self.assertRaises(RuntimeError):
                drill.activation(self.current, original, dict(sha=drill.BAD))
        state.assert_not_called()

    def test_changed_baseline_prevents_activation(self):
        (drill.STATE/'current.json').write_text(json.dumps(dict(sha='c'*40, image_id='other-image')))
        original = Mock()
        with patch.object(drill, 'command') as command:
            with self.assertRaises(RuntimeError):
                drill.activation(self.current, original, dict(sha=drill.BAD))
        original.assert_not_called()
        command.assert_not_called()

    def test_unexpected_candidate_success_is_not_held(self):
        original = Mock(return_value='unexpected healthy')
        with patch.object(drill, 'command'), patch.object(drill, 'bad_state') as state:
            with self.assertRaises(RuntimeError):
                drill.activation(self.current, original, dict(sha=drill.BAD))
        state.assert_not_called()


if __name__ == '__main__':
    unittest.main()
