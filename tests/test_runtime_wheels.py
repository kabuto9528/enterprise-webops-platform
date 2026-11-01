"""Reject changed dependency bytes or manifests before invoking Docker."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='Linux release engine imports flock')
if sys.platform != 'win32':
    spec = importlib.util.spec_from_file_location('wheel_engine', Path(__file__).parents[1] / 'scripts/webops-release.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)


def fixture(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'docker').mkdir(parents=True)
    requirements = b'demo==1.0\n'
    (source / 'requirements.txt').write_bytes(requirements)
    payload = b'opaque wheel fixture'
    digest = hashlib.sha256(payload).hexdigest()
    data = {'schema': 'webops-runtime-wheels/v1', 'requirements_sha256': hashlib.sha256(requirements).hexdigest(),
            'files': [{'name': 'demo', 'version': '1.0', 'filename': 'demo-1.0-py3-none-any.whl', 'sha256': digest}]}
    raw = (json.dumps(data) + '\n').encode()
    (source / 'docker/runtime-wheels.lock.json').write_bytes(raw)
    (source / 'docker/runtime-requirements.lock.txt').write_text('demo==1.0 --hash=sha256:' + digest + '\n')
    cache = tmp_path / 'cache'
    directory = cache / hashlib.sha256(raw).hexdigest()
    directory.mkdir(parents=True)
    wheel = directory / data['files'][0]['filename']
    wheel.write_bytes(payload)
    monkeypatch.setattr(engine, 'WHEEL_CACHE', cache)
    return source, directory, wheel


def test_valid_locked_cache_is_selected(tmp_path, monkeypatch):
    source, directory, _ = fixture(tmp_path, monkeypatch)
    assert engine.wheel_context(source)[0] == directory


def test_tampered_wheel_fails_before_build(tmp_path, monkeypatch):
    source, _, wheel = fixture(tmp_path, monkeypatch)
    wheel.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='digest mismatch'):
        engine.wheel_context(source)


def test_version_or_hash_lock_drift_is_rejected(tmp_path, monkeypatch):
    source, _, _ = fixture(tmp_path, monkeypatch)
    (source / 'requirements.txt').write_text('demo==2.0\n')
    with pytest.raises(ValueError, match='requirements.txt'):
        engine.wheel_context(source)


def test_legacy_source_keeps_existing_behavior(tmp_path):
    assert engine.wheel_context(tmp_path) is None
