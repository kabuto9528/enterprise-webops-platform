"""Linux-only isolated regressions; none of these tests deploy or require root."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Linux root executor imports flock/grp')
if sys.platform != 'win32':
    spec = importlib.util.spec_from_file_location('ci', Path(__file__).parents[1] / 'scripts/webops-ci-release.py')
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)


def test_core_tree_result_reads_only_embedded_json(tmp_path):
    state = {'current': {'sha': 'a' * 40}, 'runtime': {}, 'attempt': {}}
    (tmp_path / 'web01').write_text(json.dumps({'rc': 0, 'stdout': json.dumps(state)}))
    assert ci.parse_state_tree(tmp_path) == state


@pytest.mark.parametrize('task', [
    {'rc': 1, 'stdout': '{}'}, {'rc': 0, 'failed': True, 'stdout': '{}'},
    {'rc': 0, 'stdout': 'localhost | SUCCESS => {}'}, {'rc': 0, 'stdout': '[]'},
])
def test_tree_rejects_failed_or_unstructured_output(tmp_path, task):
    (tmp_path / 'web01').write_text(json.dumps(task))
    with pytest.raises((ValueError, KeyError, AttributeError)):
        ci.parse_state_tree(tmp_path)


def test_tree_rejects_multiple_hosts(tmp_path):
    for name in ('web01', 'unexpected'):
        (tmp_path / name).write_text('{}')
    with pytest.raises(ValueError, match='exactly one'):
        ci.parse_state_tree(tmp_path)


def test_capture_stdout_is_available_to_git_callers(tmp_path):
    with patch.object(ci, 'BASE', tmp_path):
        result = ci.run([sys.executable, '-c', 'print("captured")'], timeout=5)
    assert result.stdout.strip() == 'captured'


@pytest.mark.parametrize('mode,expected', [('healthy', 'succeeded'), ('http503', 'failed'),
                                         ('recovered', 'failed'), ('disklow', 'failed'), ('taglimit', 'failed')])
def test_deploy_preserves_failure_and_verifies_full_runtime(tmp_path, mode, expected):
    target, old = 'b' * 40, 'a' * 40
    running = old if mode == 'recovered' else target
    current = {'sha': running, 'known_healthy': True, 'archive_sha256': 'd' * 64}
    runtime = {'image_revision': running, 'version_env': running, 'docker_health': 'healthy',
               'image_id': 'image1', 'tag_image_id': 'image1', 'image_tag': 'webops-app:' + running}
    state = {'current': current, 'runtime': runtime,
             'attempt': {'sha': target, 'previous': old,
                         'status': 'failed' if mode == 'recovered' else 'succeeded',
                         'recovery': 'succeeded' if mode == 'recovered' else 'not_needed'},
             'capacity': {'docker_free_bytes': 10 * 1024**3, 'project_image_tags': 3}}
    if mode == 'disklow':
        state['capacity']['docker_free_bytes'] = 2 * 1024**3 - 1
    if mode == 'taglimit':
        state['capacity']['project_image_tags'] = 50
    external = {'matches_sha': True, 'healthz': 503 if mode == 'http503' else 200,
                'version_status': 200, 'version_value': running}
    completed = subprocess.CompletedProcess([], 2 if mode == 'recovered' else 0, '')
    git = subprocess.CompletedProcess([], 0, target + '\n')
    with patch.object(ci, 'LOGS', tmp_path), patch.object(ci, 'git', return_value=git), \
         patch.object(ci, 'wait_for_target', return_value=(True, 1)), \
         patch.object(ci, 'read_state', return_value=state), patch.object(ci, 'run', return_value=completed) as runner, \
         patch.object(ci, 'external_check', return_value=external), patch.object(ci, 'write_json'), \
         patch.object(ci, 'prune_logs'):
        result = ci.deploy('7', target)
    assert result['status'] == expected
    if mode in {'disklow', 'taglimit'}:
        runner.assert_not_called()
    if mode == 'recovered':
        assert result['recovery'] == 'succeeded'
        assert result['current_sha'] == old
        assert result['ansible_rc'] == 2


def test_retention_bounds_each_executor_record_type(tmp_path):
    locations = {name: tmp_path / name for name in ('logs', 'results', 'processed', 'pipeline')}
    for name, directory in locations.items():
        directory.mkdir()
        for number in range(34):
            (directory / (str(number) + ('.log' if name in {'logs', 'pipeline'} else '.json'))).write_text('x')
    with patch.object(ci, 'BASE', tmp_path), patch.object(ci, 'LOGS', locations['logs']), \
         patch.object(ci, 'RESULTS', locations['results']), patch.object(ci, 'PROCESSED', locations['processed']):
        ci.prune_logs()
    assert all(len(list(path.iterdir())) == 30 for path in locations.values())
