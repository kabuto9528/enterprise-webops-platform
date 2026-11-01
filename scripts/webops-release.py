#!/usr/bin/python3
"""Root-owned release engine. Only reviewed Git archives may be supplied by an admin.

No sudo rule for webops-deploy is installed. Docker builds are privileged work.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

ROOT = Path('/opt/webops/releases')
STATE = Path('/var/lib/webops-release')
CURRENT = Path('/opt/webops/current')
BASE = os.environ.get(
    'WEBOPS_BASE_IMAGE',
    'docker.m.daocloud.io/library/python:3.12.14-slim-bookworm@'
    'sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e',
)
SHA = re.compile(r'[0-9a-f]{40}')
WHEEL_CACHE = Path('/opt/webops/dependencies/runtime-wheels')


def run(args, timeout=15, log_path=None):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired.output can be bytes even when text=True. Retain the
        # partial build output before propagating the original failure.
        output = error.output or b''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        if log_path:
            log_path.write_text(output + f'\n[command timed out after {timeout}s]\n', encoding='utf-8')
            os.chmod(log_path, 0o600)
        elif output:
            print(output, end='', flush=True)
        raise
    if log_path:
        log_path.write_text(result.stdout)
        os.chmod(log_path, 0o600)
    if result.returncode:
        print(result.stdout, end='', flush=True)
        raise RuntimeError(f'{args[0]} exited {result.returncode}')
    return result.stdout


def atomic_json(path, value):
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def version_file(sha):
    if not SHA.fullmatch(sha):
        raise ValueError('Expected full lowercase Git SHA')
    return STATE / 'versions' / (sha + '.json')


def read_version(sha):
    data = json.loads(version_file(sha).read_text())
    if data['sha'] != sha or not data.get('known_healthy'):
        raise RuntimeError('Version is not a saved healthy release')
    inspect_image(data['image_id'], sha)
    return data


def inspect_image(image, sha):
    info = json.loads(run(['docker', 'image', 'inspect', image]))[0]
    if info['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != sha:
        raise RuntimeError('Image revision does not match SHA')
    return info['Id']


def compose_file(directory, image, sha):
    # The candidate archive cannot override runtime privileges, ports or mounts.
    config = {
        'name': 'webops', 'services': {'app': {
            'image': image, 'pull_policy': 'never', 'platform': 'linux/amd64',
            'environment': {'WEBOPS_VERSION': sha},
            'ports': ['127.0.0.1:18080:8000'], 'restart': 'unless-stopped',
            'stop_grace_period': '20s', 'user': '10001:10001',
            'read_only': True, 'tmpfs': ['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777'],
            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'mem_limit': '256m', 'cpus': 0.5, 'pids_limit': 100,
            'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
            'networks': ['app']}},
        'networks': {'app': {'name': 'webops-app', 'driver': 'bridge',
                            'driver_opts': {'com.docker.network.bridge.name': 'br-webops'}}}}
    path = directory / 'compose.json'
    atomic_json(path, config)
    run(['docker', 'compose', '-p', 'webops', '-f', str(path), 'config', '--quiet'])
    return str(path)


def compose(data, *args, timeout=120):
    return run(['docker', 'compose', '-p', 'webops', '-f', data['compose'], *args], timeout)


def health(data, attempts=10):
    last = None
    for attempt in range(attempts):
        try:
            ids = compose(data, 'ps', '-q', 'app', timeout=5).split()
            if len(ids) != 1:
                raise RuntimeError('Expected exactly one project app container')
            item = json.loads(run(['docker', 'inspect', ids[0]]))[0]
            if item['Image'] != data['image_id']:
                raise RuntimeError('Running image differs from saved image ID')
            if item['State'].get('Health', {}).get('Status') != 'healthy':
                raise RuntimeError('Docker health has not become healthy')
            for port in (18080, 80):
                for route, expected in (('healthz', {'status': 'ok'}),
                                        ('version', {'version': data['sha']})):
                    # Local checks must not inherit proxy environment settings.
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(f'http://127.0.0.1:{port}/{route}', timeout=3) as reply:
                        if reply.status != 200 or json.load(reply) != expected:
                            raise RuntimeError(f'Unexpected response on {port}/{route}')
            return {'docker': 'healthy', 'loopback': 'passed', 'nginx': 'passed',
                    'version': data['sha'], 'attempts': attempt + 1}
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(3)
    raise RuntimeError(f'Health checks exhausted: {last}')


def activate(data):
    inspect_image(data['image_id'], data['sha'])
    compose(data, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '90')
    return health(data)


def set_current(data):
    # Called only after both local and Nginx health/version checks pass.
    link = CURRENT
    if link.exists() and not link.is_symlink():
        raise RuntimeError('current exists and is not a symlink')
    previous_target = os.readlink(link) if link.is_symlink() else None
    temporary = link.with_name('current-' + uuid.uuid4().hex)
    temporary.symlink_to(Path(data['compose']).parent)
    os.replace(temporary, link)
    try:
        atomic_json(STATE / 'current.json', data)
    except Exception as error:
        try:
            if previous_target is None:
                link.unlink()
            else:
                restore = link.with_name('current-restore-' + uuid.uuid4().hex)
                restore.symlink_to(previous_target)
                os.replace(restore, link)
        except Exception as restore_error:
            raise RuntimeError(f'current metadata failed: {error}; pointer restore failed: {restore_error}') from error
        raise


def unpack(archive, directory):
    # Refuse symlinks, hardlinks, devices and traversal before extraction as root.
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > 100 * 1024 * 1024:
            raise ValueError('Archive exceeds project limits')
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Archive contains an unsafe path or file type')
        source.extractall(directory, members=members, filter='data')
    for path in ('Dockerfile', 'requirements.txt', 'app/main.py'):
        if not (directory / path).is_file():
            raise ValueError(f'Missing source file: {path}')


def wheel_context(source):
    """Verify every external build input against the reviewed source manifest."""
    manifest_path = source / 'docker' / 'runtime-wheels.lock.json'
    if not manifest_path.exists():
        return None  # compatibility with saved pre-Stage11 source revisions
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get('schema') != 'webops-runtime-wheels/v1':
        raise ValueError('Unsupported wheel manifest')
    if hashlib.sha256((source / 'requirements.txt').read_bytes()).hexdigest() != manifest.get('requirements_sha256'):
        raise ValueError('Wheel manifest does not match requirements.txt')
    fingerprint = hashlib.sha256(raw).hexdigest()
    cache = WHEEL_CACHE / fingerprint
    if cache.is_symlink() or not cache.is_dir():
        raise ValueError('Verified offline wheel cache is missing')
    entries = manifest.get('files') or []
    if not 1 <= len(entries) <= 100:
        raise ValueError('Invalid wheel manifest size')
    lock_lines = []
    names = set()
    for item in entries:
        name, digest = item.get('filename', ''), item.get('sha256', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', name) or '..' in name
                or name in names or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('Unsafe or duplicate wheel manifest entry')
        names.add(name)
        path = cache / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Offline wheel digest mismatch')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', item['name'])
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', item['version'])):
            raise ValueError('Invalid wheel requirement identity')
        lock_lines.append('%s==%s --hash=sha256:%s\n' % (item['name'], item['version'], digest))
    pins = {line.strip() for line in (source / 'requirements.txt').read_text().splitlines()
            if line.strip() and not line.startswith('#')}
    if pins != {item['name'] + '==' + item['version'] for item in entries}:
        raise ValueError('Wheel versions differ from application requirements')
    if (source / 'docker' / 'runtime-requirements.lock.txt').read_text() != ''.join(lock_lines):
        raise ValueError('pip hash lock does not match wheel manifest')
    return cache, fingerprint


def prepare(sha, archive, expected_digest):
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise ValueError('Archive checksum mismatch')
    # Unique attempts allow a failed SHA to be retried without deleting evidence.
    directory = ROOT / sha / 'stage06' / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o750)
    source = directory / 'source'
    source.mkdir(mode=0o750)
    unpack(archive, source)
    wheels = wheel_context(source)
    options = (['--network=none', '--build-context', 'runtime_wheels=' + str(wheels[0])]
               if wheels else ['--network=default'])
    run(['docker', 'build', '--pull=false', *options,
         '--build-arg', 'PYTHON_BASE=' + BASE, '--build-arg', 'WEBOPS_VERSION=' + sha,
         '--label', 'org.opencontainers.image.revision=' + sha,
         '-t', 'webops-app:' + sha, str(source)], 900, directory / 'build.log')
    image = inspect_image('webops-app:' + sha, sha)
    return {'sha': sha, 'image_id': image, 'archive_sha256': digest,
            'wheel_manifest_sha256': wheels[1] if wheels else None,
            'compose': compose_file(directory, image, sha), 'known_healthy': False}


def adopt(sha):
    # Explicit bootstrap: verify the existing Compose project before saving recovery data.
    if (STATE / 'current.json').exists():
        raise RuntimeError('Already adopted; refusing to overwrite recovery state')
    ids = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=webops',
               '--filter', 'label=com.docker.compose.service=app']).split()
    if len(ids) != 1:
        raise RuntimeError('Cannot identify the existing project app')
    container = json.loads(run(['docker', 'inspect', ids[0]]))[0]
    image = inspect_image(container['Image'], sha)
    if version_file(sha).exists():
        # A previous adoption may have saved the healthy record but failed
        # while updating current. Reuse it only if the live image still matches.
        data = read_version(sha)
        if data.get('adopted') is not True or data['image_id'] != image:
            raise RuntimeError('Saved adoption does not match the running image')
        data['health'] = health(data)
        set_current(data)
        return data
    directory = ROOT / sha / 'stage06' / 'adopted'
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    data = {'sha': sha, 'image_id': image, 'known_healthy': True,
            'compose': compose_file(directory, image, sha), 'adopted': True}
    result = health(data)
    data['health'] = result
    atomic_json(version_file(sha), data)
    set_current(data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['adopt', 'deploy', 'rollback'])
    parser.add_argument('sha')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--archive-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Requires an explicitly authorized administrative connection')
    version_file(args.sha)  # Validate before touching paths.
    if args.action == 'deploy' and (not args.archive or
            not re.fullmatch(r'[0-9a-f]{64}', args.archive_sha256 or '')):
        parser.error('deploy requires archive and its SHA256')
    STATE.mkdir(mode=0o700, exist_ok=True)
    (STATE / 'versions').mkdir(mode=0o700, exist_ok=True)
    (STATE / 'attempts').mkdir(mode=0o700, exist_ok=True)
    with (STATE / 'release.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another release holds the lock', file=sys.stderr)
            return 75
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex
        attempt = {'action': args.action, 'sha': args.sha, 'started_utc': stamp,
                   'status': 'running', 'previous': None, 'recovery': 'not_needed'}
        record = STATE / 'attempts' / (stamp + '.json')
        atomic_json(record, attempt)
        previous = None
        switched = False
        try:
            run(['nginx', '-t'])
            if args.action == 'adopt':
                data = adopt(args.sha)
            else:
                # Deploy verifies its previous version; rollback verifies its target.
                current = json.loads((STATE / 'current.json').read_text())
                attempt['previous'] = current['sha']
                if args.action == 'rollback':
                    # A missing current image must not prevent restoration of a
                    # separately verified saved target. It does mean that a
                    # failed restore has no verified fallback to the current image.
                    try:
                        previous = read_version(current['sha'])
                    except Exception as error:
                        attempt['previous_unavailable'] = str(error)
                    data = read_version(args.sha)
                else:
                    previous = read_version(current['sha'])
                    health(previous)
                    # Reuse a saved image instead of replacing the only healthy
                    # record when the same commit is deployed again.
                    if version_file(args.sha).exists():
                        data = read_version(args.sha)
                    else:
                        data = prepare(args.sha, args.archive, args.archive_sha256)
                attempt['image_id'] = data['image_id']
                attempt['status'] = 'switching'
                atomic_json(record, attempt)
                switched = True  # up can partially replace a container before reporting failure.
                result = activate(data)
                data['health'] = result
                data['known_healthy'] = True
                atomic_json(version_file(args.sha), data)
                set_current(data)
            attempt.update(status='succeeded', image_id=data['image_id'], health=data['health'])
            return 0
        except Exception as error:
            attempt.update(status='failed', error=str(error))
            if switched and previous:
                try:
                    attempt['recovery_health'] = activate(previous)
                    set_current(previous)
                    attempt['recovery'] = 'succeeded'
                except Exception as recovery_error:
                    attempt.update(recovery='failed', recovery_error=str(recovery_error))
            elif previous:
                attempt['recovery'] = 'not_switched'
            else:
                attempt['recovery'] = 'no_verified_previous'
            print(json.dumps(attempt), file=sys.stderr)
            return 1  # Recovery never changes failed deploy into successful deploy.
        finally:
            attempt['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_json(record, attempt)


if __name__ == '__main__':
    sys.exit(main())
