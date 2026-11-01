"""Isolated failure-state checks; these do not count as target-host acceptance."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

if sys.platform != 'win32':
    spec = importlib.util.spec_from_file_location(
        'release_engine', Path(__file__).parents[1] / 'scripts' / 'webops-release.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)


@unittest.skipIf(sys.platform == 'win32', 'Linux flock is required; run on ops01')
class ReleaseStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.previous = {'sha': 'a' * 40, 'image_id': 'sha256:old',
                         'compose': '/saved/compose.json', 'known_healthy': True}
        self.candidate = {'sha': 'b' * 40, 'image_id': 'sha256:new',
                          'compose': '/new/compose.json', 'known_healthy': False}
        (self.state / 'current.json').write_text(json.dumps(self.previous))
        self.patches = [patch.object(engine, 'STATE', self.state),
                        patch.object(engine.os, 'geteuid', return_value=0),
                        patch.object(engine, 'run', return_value=''),
                        patch.object(engine, 'read_version', return_value=self.previous),
                        patch.object(engine, 'health', return_value={'version': 'a' * 40}),
                        patch.object(engine, 'set_current')]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def invoke(self):
        with patch.object(sys, 'argv', ['release', 'deploy', 'b' * 40,
                                       '--archive', '/source.tar',
                                       '--archive-sha256', 'c' * 64]):
            return engine.main()

    def record(self):
        return json.loads(next((self.state / 'attempts').glob('*.json')).read_text())

    def test_failed_health_recovers_but_returns_failure(self):
        with patch.object(engine, 'prepare', return_value=self.candidate), \
             patch.object(engine, 'activate', side_effect=[RuntimeError('bad health'), {'ok': True}]) as activate:
            self.assertEqual(self.invoke(), 1)
            self.assertEqual(activate.call_args_list[1].args[0], self.previous)
        self.assertEqual(self.record()['status'], 'failed')
        self.assertEqual(self.record()['recovery'], 'succeeded')

    def test_recovery_failure_remains_failure(self):
        with patch.object(engine, 'prepare', return_value=self.candidate), \
             patch.object(engine, 'activate', side_effect=RuntimeError('unavailable')):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.record()['recovery'], 'failed')
        engine.set_current.assert_not_called()

    def test_build_failure_never_switches(self):
        with patch.object(engine, 'prepare', side_effect=RuntimeError('build failed')), \
             patch.object(engine, 'activate') as activate:
            self.assertEqual(self.invoke(), 1)
            activate.assert_not_called()
        self.assertEqual(self.record()['recovery'], 'not_switched')

    def test_no_previous_is_reported_without_claiming_recovery(self):
        (self.state / 'current.json').unlink()
        with patch.object(engine, 'activate') as activate:
            self.assertEqual(self.invoke(), 1)
            activate.assert_not_called()
        self.assertEqual(self.record()['recovery'], 'no_verified_previous')

    def test_success_saves_health_and_current_after_activation(self):
        with patch.object(engine, 'prepare', return_value=self.candidate), \
             patch.object(engine, 'activate', return_value={'version': 'b' * 40}):
            self.assertEqual(self.invoke(), 0)
        saved = json.loads((self.state / 'versions' / ('b' * 40 + '.json')).read_text())
        self.assertTrue(saved['known_healthy'])
        engine.set_current.assert_called_once()
        self.assertEqual(self.record()['status'], 'succeeded')

    def test_concurrent_attempt_is_refused(self):
        with patch.object(engine.fcntl, 'flock', side_effect=BlockingIOError):
            self.assertEqual(self.invoke(), 75)
        self.assertEqual(list((self.state / 'attempts').glob('*.json')), [])

    def test_manual_rollback_does_not_require_broken_current_to_be_healthy(self):
        with patch.object(sys, 'argv', ['release', 'rollback', 'a' * 40]), \
             patch.object(engine, 'activate', return_value={'version': 'a' * 40}), \
             patch.object(engine, 'health', side_effect=RuntimeError('current is down')) as health:
            self.assertEqual(engine.main(), 0)
            health.assert_not_called()

    def test_manual_rollback_can_restore_target_when_current_image_is_missing(self):
        target = {'sha': 'b' * 40, 'image_id': 'sha256:known-good',
                  'compose': '/saved/compose.json', 'known_healthy': True}
        with patch.object(sys, 'argv', ['release', 'rollback', target['sha']]), \
             patch.object(engine, 'read_version', side_effect=[RuntimeError('current image missing'), target]), \
             patch.object(engine, 'activate', return_value={'version': target['sha']}) as activate:
            self.assertEqual(engine.main(), 0)
            activate.assert_called_once_with(target)
        self.assertEqual(self.record()['status'], 'succeeded')
        self.assertEqual(self.record()['previous'], 'a' * 40)

    def test_failed_rollback_without_recoverable_previous_stays_failed(self):
        target = {'sha': 'b' * 40, 'image_id': 'sha256:known-good',
                  'compose': '/saved/compose.json', 'known_healthy': True}
        with patch.object(sys, 'argv', ['release', 'rollback', target['sha']]), \
             patch.object(engine, 'read_version', side_effect=[RuntimeError('current image missing'), target]), \
             patch.object(engine, 'activate', side_effect=RuntimeError('target unhealthy')) as activate:
            self.assertEqual(engine.main(), 1)
            activate.assert_called_once_with(target)
        self.assertEqual(self.record()['status'], 'failed')
        self.assertEqual(self.record()['recovery'], 'no_verified_previous')
        self.assertEqual(self.record()['previous_unavailable'], 'current image missing')
        engine.set_current.assert_not_called()

    def test_bad_sha_does_not_create_state(self):
        with patch.object(sys, 'argv', ['release', 'deploy', '../../bad']):
            with self.assertRaises(ValueError):
                engine.main()
        self.assertFalse((self.state / 'attempts').exists())

    def test_redeploying_saved_sha_reuses_the_recoverable_image(self):
        versions = self.state / 'versions'
        versions.mkdir()
        (versions / ('a' * 40 + '.json')).write_text(json.dumps(self.previous))
        with patch.object(sys, 'argv', ['release', 'deploy', 'a' * 40,
                                       '--archive', '/source.tar',
                                       '--archive-sha256', 'c' * 64]), \
             patch.object(engine, 'prepare') as prepare, \
             patch.object(engine, 'activate', return_value={'version': 'a' * 40}):
            self.assertEqual(engine.main(), 0)
            prepare.assert_not_called()
        saved = json.loads((versions / ('a' * 40 + '.json')).read_text())
        self.assertEqual(saved['image_id'], 'sha256:old')

    def test_archive_traversal_is_rejected_before_extract(self):
        archive = self.root / 'bad.tar'
        with tarfile.open(archive, 'w') as output:
            info = tarfile.TarInfo('../escaped')
            info.size = 1
            output.addfile(info, io.BytesIO(b'x'))
        target = self.root / 'extract'
        target.mkdir()
        with self.assertRaises(ValueError):
            engine.unpack(archive, target)
        self.assertFalse((self.root / 'escaped').exists())


@unittest.skipIf(sys.platform == 'win32', 'Linux symlink behavior is required; run on ops01')
class CurrentPointerTests(unittest.TestCase):
    def test_metadata_failure_restores_existing_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            link = root / 'current'
            link.symlink_to('previous-release')
            with patch.object(engine, 'CURRENT', link), \
                 patch.object(engine, 'STATE', root), \
                 patch.object(engine, 'atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    engine.set_current({'compose': str(root / 'new' / 'compose.json')})
            self.assertEqual(link.readlink(), Path('previous-release'))

    def test_metadata_failure_removes_only_new_pointer_when_none_existed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            link = root / 'current'
            with patch.object(engine, 'CURRENT', link), \
                 patch.object(engine, 'STATE', root), \
                 patch.object(engine, 'atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    engine.set_current({'compose': str(root / 'new' / 'compose.json')})
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink())


@unittest.skipIf(sys.platform == 'win32', 'Linux release runtime is required; run on ops01')
class AdoptResumeTests(unittest.TestCase):
    def test_retry_after_metadata_failure_keeps_existing_healthy_image(self):
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / 'state'
            (state / 'versions').mkdir(parents=True)
            image = 'sha256:known-healthy'
            compose_path = str(root / 'release' / 'compose.json')
            output = ['container-id\n', json.dumps([{'Image': image}])] * 2
            with patch.object(engine, 'ROOT', root / 'releases'), \
                 patch.object(engine, 'STATE', state), \
                 patch.object(engine, 'run', side_effect=output), \
                 patch.object(engine, 'inspect_image', return_value=image), \
                 patch.object(engine, 'compose_file', return_value=compose_path) as compose, \
                 patch.object(engine, 'health', return_value={'version': sha}), \
                 patch.object(engine, 'set_current', side_effect=[OSError('disk full'), None]) as current:
                with self.assertRaises(OSError):
                    engine.adopt(sha)
                saved_path = state / 'versions' / (sha + '.json')
                original_record = saved_path.read_text()
                self.assertEqual(engine.adopt(sha)['image_id'], image)
                self.assertEqual(saved_path.read_text(), original_record)
                self.assertEqual(compose.call_count, 1)
                self.assertEqual(current.call_count, 2)

    def test_retry_refuses_if_live_image_changed(self):
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'versions').mkdir()
            data = {'sha': sha, 'image_id': 'sha256:saved', 'known_healthy': True,
                    'adopted': True, 'compose': '/saved/compose.json'}
            (state / 'versions' / (sha + '.json')).write_text(json.dumps(data))
            output = ['container-id\n', json.dumps([{'Image': 'sha256:different'}])]
            with patch.object(engine, 'STATE', state), \
                 patch.object(engine, 'run', side_effect=output), \
                 patch.object(engine, 'inspect_image', side_effect=lambda image, _: image), \
                 patch.object(engine, 'set_current') as current:
                with self.assertRaises(RuntimeError):
                    engine.adopt(sha)
                current.assert_not_called()


@unittest.skipIf(sys.platform == 'win32', 'Linux release runtime is required; run on ops01')
class CommandTimeoutTests(unittest.TestCase):
    def test_timeout_preserves_partial_output_and_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'build.log'
            with self.assertRaises(subprocess.TimeoutExpired):
                engine.run([sys.executable, '-u', '-c',
                            "import time; print('partial build output'); time.sleep(3)"],
                           timeout=0.5, log_path=log)
            self.assertIn('partial build output', log.read_text(encoding='utf-8'))
            self.assertIn('timed out', log.read_text(encoding='utf-8'))
            self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
