#!/usr/bin/python3
"""Stage 11 root-owned CI release executor for the Enterprise WebOps Platform.

The unprivileged Jenkins user can only create a small request file. This program
is started by the path unit ``webops-ci-release.path`` when such a file appears,
accepts **no arguments**, validates every field and then deploys exactly one
commit from the trusted bare repository.

Boundary: the only privileged interface is "deploy commit X, which is already an
ancestor of origin/main in /srv/git/webops.git". Jenkins supplies nothing but
that SHA and a build number; it holds no SSH key, no sudo rule and no target
credentials. Playbook code is read from a root-owned clone, never from the
Jenkins workspace, so a compromised workspace cannot change what root executes.
"""

import datetime as dt
import fcntl
import grp
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BASE = Path('/var/lib/webops-ci')
REQUESTS = BASE / 'requests'
RESULTS = BASE / 'results'
PROCESSED = BASE / 'processed'
LOGS = BASE / 'logs'
STATUS = BASE / 'status'
WORK = BASE / 'work'
TMP = BASE / 'tmp'
CONFIG = Path('/etc/webops/ci')
INVENTORY = CONFIG / 'inventory.yml'
ANSIBLE_CONFIG = CONFIG / 'ansible.cfg'
ENDPOINT_FILE = CONFIG / 'endpoint'
ANSIBLE_PLAYBOOK = '/opt/webops/venvs/jenkins-ansible/bin/ansible-playbook'
ANSIBLE = '/opt/webops/venvs/jenkins-ansible/bin/ansible'
SHA_RE = re.compile(r'[0-9a-f]{40}')
BUILD_RE = re.compile(r'[0-9]{1,7}')
REQUEST_MAX_AGE = 3600
MAX_REQUESTS_PER_RUN = 3
DEPLOY_TIMEOUT = 3900
MAX_LOGS = 30
GROUP = 'webops-ci'


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def group_id():
    return grp.getgrnam(GROUP).gr_gid


def write_json(path, value, mode=0o640, chown_group=True):
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.chmod(temporary, mode)
    if chown_group:
        os.chown(temporary, 0, group_id())
    os.replace(temporary, path)


def base_env():
    # ssh/ansible must not inherit a polluted environment from the caller.
    return {
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': '/root',
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'ANSIBLE_CONFIG': str(ANSIBLE_CONFIG),
        'ANSIBLE_SSH_USETTY': 'False',
        'ANSIBLE_SSH_RETRIES': '3',
        'ANSIBLE_LOCAL_TEMP': str(TMP),
        'ANSIBLE_NOCOLOR': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }


def run(args, timeout, stdout=None, extra_env=None):
    environment = base_env()
    if extra_env:
        environment.update(extra_env)
    # Capture by default: callers read .stdout, and inheriting the journal
    # stream would make those reads return None.
    return subprocess.run(args, env=environment, text=True,
                          stdout=subprocess.PIPE if stdout is None else stdout,
                          stderr=subprocess.STDOUT, timeout=timeout, cwd=str(BASE))


def git(*args, timeout=30):
    return run(['git', '-C', str(WORK), *args], timeout)


def jenkins_uid():
    return pwd.getpwnam('jenkins').pw_uid


def read_request(path):
    """Return (build, sha) or raise ValueError with a stable reason."""
    stat_result = os.lstat(path)
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError('request is not a regular file')
    if stat_result.st_uid != jenkins_uid():
        raise ValueError('request owner is not jenkins')
    if stat_result.st_mode & 0o022:
        raise ValueError('request is group or world writable')
    if stat_result.st_size > 4096:
        raise ValueError('request too large')
    if time.time() - stat_result.st_mtime > REQUEST_MAX_AGE:
        raise ValueError('request too old')
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        raw = os.read(handle, 4096)
    finally:
        os.close(handle)
    document = json.loads(raw.decode('utf-8'))
    if not isinstance(document, dict):
        raise ValueError('request is not an object')
    build = str(document.get('build', ''))
    sha = str(document.get('sha', ''))
    if not BUILD_RE.fullmatch(build) or path.stem != build:
        raise ValueError('request build number mismatch')
    if not SHA_RE.fullmatch(sha):
        raise ValueError('request sha is not a full lowercase commit id')
    return build, sha


TARGET_STATE_SCRIPT = r'''
import json, pathlib, shutil, subprocess, sys
state = pathlib.Path('/var/lib/webops-release')
def load(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}
def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=15)
current = load(state / 'current.json')
attempt = {}
for path in sorted((state / 'attempts').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True):
    candidate = load(path)
    if candidate.get('sha') == sys.argv[1] and candidate.get('action') == 'deploy':
        attempt = candidate
        break
ids = docker('ps', '-q', '--filter', 'label=com.docker.compose.project=webops',
             '--filter', 'label=com.docker.compose.service=app').split()
runtime = {}
if len(ids) == 1:
    item = json.loads(docker('inspect', ids[0]))[0]
    config = item.get('Config') or {}
    versions = [s.split('=',1)[1] for s in config.get('Env',[]) if s.startswith('WEBOPS_VERSION=')]
    tag = 'webops-app:' + str(current.get('sha',''))
    try: tag_id = json.loads(docker('image', 'inspect', tag))[0]['Id']
    except (subprocess.SubprocessError, KeyError, ValueError): tag_id = None
    runtime = {'image_id': item.get('Image'), 'image_ref': config.get('Image'),
               'image_revision': config.get('Labels',{}).get('org.opencontainers.image.revision'),
               'version_env': versions[0] if len(versions)==1 else None,
               'docker_health': item.get('State',{}).get('Health',{}).get('Status'),
               'image_tag': tag, 'tag_image_id': tag_id}
images = docker('image', 'ls', '-q', 'webops-app').splitlines()
print(json.dumps({'current':current, 'attempt':attempt, 'runtime':runtime,
                  'capacity':{'docker_free_bytes':shutil.disk_usage('/var/lib/docker').free,
                              'project_image_tags':len(images)}}))
'''


def parse_state_tree(directory):
    """The core tree callback writes one task-result JSON per inventory host."""
    documents = [path for path in directory.iterdir() if path.is_file()]
    if len(documents) != 1:
        raise ValueError('expected exactly one target result')
    task = json.loads(documents[0].read_text(encoding='utf-8'))
    if task.get('rc') != 0 or task.get('failed') or task.get('unreachable'):
        raise ValueError('target state task failed')
    state = json.loads(task['stdout'])
    if not isinstance(state, dict) or not isinstance(state.get('current'), dict):
        raise ValueError('invalid target state document')
    return state


def read_state(sha):
    """Read only explicit JSON emitted by a fixed remote command, via core --tree."""
    command = 'python3 -c %s %s' % (shlex.quote(TARGET_STATE_SCRIPT), shlex.quote(sha))
    try:
        with tempfile.TemporaryDirectory(prefix='state-', dir=TMP) as directory:
            result = run([ANSIBLE, '-i', str(INVENTORY), 'web', '-b', '-m',
                          'ansible.builtin.command', '-a', command, '--tree', directory], timeout=180)
            if result.returncode != 0:
                return {'error': 'state read exited %s' % result.returncode}
            return parse_state_tree(Path(directory))
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError):
        return {'error': 'state read failed; inspect the private executor log'}


def target_host():
    try:
        endpoint = ENDPOINT_FILE.read_text(encoding='utf-8').strip().rstrip('/')
    except OSError:
        return None
    return endpoint.split('//')[-1].split('/')[0].split(':')[0]


def wait_for_target(attempts=9, delay=10, port=22):
    """Bounded wait for the deploy target's SSH port; a transient tailnet drop
    must not be recorded as a failed deployment."""
    import socket
    host = target_host()
    if not host:
        return False, 'endpoint file missing'
    last = None
    for attempt in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=5):
                return True, attempt + 1
        except OSError as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(delay)
    return False, str(last)


def external_check(expected_sha):
    try:
        endpoint = ENDPOINT_FILE.read_text(encoding='utf-8').strip().rstrip('/')
    except OSError:
        return {'error': 'endpoint file missing'}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {'healthz': None, 'version_status': None, 'version_value': None,
              'matches_sha': False}
    for attempt in range(5):
        try:
            with opener.open(endpoint + '/healthz', timeout=5) as reply:
                report['healthz'] = reply.status
                body = json.load(reply)
            with opener.open(endpoint + '/version', timeout=5) as reply:
                report['version_status'] = reply.status
                report['version_value'] = json.load(reply).get('version')
            report['matches_sha'] = (report['version_value'] == expected_sha)
            if report['healthz'] == 200 and body == {'status': 'ok'}:
                break
        except (OSError, ValueError):
            report['error'] = 'external HTTP check failed'
        if attempt < 4:
            time.sleep(3)
    return report


def prune_logs():
    for directory, pattern in ((LOGS, '*.log'), (RESULTS, '*.json'),
                               (PROCESSED, '*.json'), (BASE / 'pipeline', '*.log')):
        paths = sorted(directory.glob(pattern), key=lambda item: item.lstat().st_mtime, reverse=True)
        for stale in paths[MAX_LOGS:]:
            try:
                stale.unlink()
            except OSError:
                pass


def move_request(path, build, tag):
    target = PROCESSED / ('%s-%s-%s.json' % (build, tag, str(int(time.time()))))
    try:
        os.replace(path, target)
    except OSError:
        pass


def deploy(build, sha):
    started = utc_now()
    log_path = LOGS / ('%s-%s.log' % (build, sha))
    result = {
        'schema': 'webops-ci-release/v1',
        'build': build,
        'sha': sha,
        'status': 'failed',
        'ansible_rc': None,
        'previous_sha': None,
        'current_sha': None,
        'recovery': 'unknown',
        'external': {},
        'runtime': None,
        'archive_sha256': None,
        'started_utc': iso(started),
        'finished_utc': None,
        'detail': '',
    }

    def log(message):
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write('[%s] %s\n' % (iso(utc_now()), message))

    log_path.touch(mode=0o600, exist_ok=True)
    try:
        fetch = git('fetch', '--prune', '--no-tags', 'origin', timeout=180)
        if fetch.returncode != 0:
            result['detail'] = 'git fetch failed'
            log('git fetch exited %s' % fetch.returncode)
            return result
        if git('cat-file', '-e', sha + '^{commit}').returncode != 0:
            result['detail'] = 'commit not present in trusted repository'
            return result
        if git('merge-base', '--is-ancestor', sha, 'refs/remotes/origin/main').returncode != 0:
            result['detail'] = 'commit is not on origin/main'
            return result
        if git('checkout', '--detach', '--force', sha).returncode != 0:
            result['detail'] = 'checkout failed'
            return result
        if git('rev-parse', 'HEAD').stdout.strip() != sha:
            result['detail'] = 'work tree HEAD does not match request'
            return result
        log('checkout=%s' % sha)

        reachable, info = wait_for_target()
        if not reachable:
            result['detail'] = 'deploy target ssh port unreachable: %s' % info
            log(result['detail'])
            return result
        log('target_reachable_attempt=%s' % info)

        before = read_state(sha)
        capacity = before.get('capacity') or {}
        if before.get('error') or not before.get('current', {}).get('known_healthy'):
            result['detail'] = 'preflight cannot verify saved healthy current release'
            return result
        if capacity.get('docker_free_bytes', 0) < 2 * 1024**3 or capacity.get('project_image_tags', 50) >= 50:
            result['detail'] = 'project image limit or 2GiB disk reserve reached; retain recovery images and review capacity'
            return result
        result['capacity_before'] = capacity

        with log_path.open('a', encoding='utf-8') as sink:
            sink.write('--- ansible-playbook deploy.yml release_sha=%s ---\n' % sha)
            sink.flush()
            try:
                completed = run([ANSIBLE_PLAYBOOK, '-i', str(INVENTORY),
                                 str(WORK / 'ansible' / 'deploy.yml'),
                                 '-e', 'release_sha=' + sha],
                                timeout=DEPLOY_TIMEOUT, stdout=sink)
                result['ansible_rc'] = completed.returncode
            except subprocess.TimeoutExpired:
                result['ansible_rc'] = 'timeout'
                result['detail'] = 'ansible deploy timed out'
        log('ansible rc=%s' % result['ansible_rc'])
        if result['ansible_rc'] != 0 and not result['detail']:
            result['detail'] = 'ansible exited %s; inspect the private executor log' % result['ansible_rc']

        state = read_state(sha) or {}
        attempt = state.get('attempt') or {}
        current = state.get('current') or {}
        runtime = state.get('runtime') or None
        result['previous_sha'] = attempt.get('previous') or None
        result['recovery'] = attempt.get('recovery') or 'unknown'
        result['current_sha'] = current.get('sha') or None
        result['runtime'] = runtime
        result['archive_sha256'] = current.get('archive_sha256')
        result['wheel_manifest_sha256'] = current.get('wheel_manifest_sha256')
        result['capacity_after'] = state.get('capacity') or {}
        if state.get('error'):
            log('state read: %s' % state['error'])

        expected = sha if result['ansible_rc'] == 0 else (result['current_sha'] or '')
        result['external'] = external_check(expected)
        log('external=%s' % json.dumps(result['external'], sort_keys=True))

        chain_ok = (bool(runtime) and runtime.get('image_revision') == sha
                    and runtime.get('version_env') == sha and runtime.get('docker_health') == 'healthy'
                    and runtime.get('tag_image_id') == runtime.get('image_id')
                    and current.get('sha') == sha and attempt.get('sha') == sha
                    and attempt.get('status') == 'succeeded')
        if (result['ansible_rc'] == 0 and result['external'].get('matches_sha')
                and result['external'].get('healthz') == 200
                and result['external'].get('version_status') == 200 and chain_ok):
            result['status'] = 'succeeded'
        elif result['ansible_rc'] == 0:
            result['status'] = 'failed'
            result['detail'] = result['detail'] or 'image label, injected version or external version does not match the commit'
        else:
            result['status'] = 'failed'
            if not result['detail']:
                result['detail'] = ('release engine reported a failed deployment (recovery=%s)'
                                    % result['recovery'])
        return result
    except Exception as error:  # raw exceptions belong only in root-private logs
        result['status'] = 'failed'
        result['detail'] = 'unexpected executor error; inspect the private executor log'
        log('unexpected executor error: %s' % error)
        return result
    finally:
        result['finished_utc'] = iso(utc_now())
        try:
            write_json(RESULTS / (build + '.json'), result)
            write_json(STATUS / 'last-release.json', result, mode=0o644, chown_group=False)
            log('status=%s' % result['status'])
        except OSError as error:
            print('cannot write result: %s' % error, file=sys.stderr)
        prune_logs()


def main():
    if os.geteuid() != 0:
        print('webops-ci-release requires root', file=sys.stderr)
        return 2
    os.umask(0o027)
    for directory in (REQUESTS, RESULTS, PROCESSED, LOGS, STATUS, TMP):
        if not directory.is_dir():
            print('missing directory %s' % directory, file=sys.stderr)
            return 2
    if not WORK.is_dir():
        print('missing root clone %s' % WORK, file=sys.stderr)
        return 2

    with (BASE / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('another executor run holds the lock', file=sys.stderr)
            return 0

        pending = sorted((item for item in REQUESTS.glob('*.json') if BUILD_RE.fullmatch(item.stem)),
                         key=lambda item: item.stat().st_mtime)
        if not pending:
            return 0
        for request_path in pending[:MAX_REQUESTS_PER_RUN]:
            build = request_path.stem
            try:
                build, sha = read_request(request_path)
            except (ValueError, OSError) as error:
                rejected = {
                    'schema': 'webops-ci-release/v1',
                    'build': build,
                    'sha': None,
                    'status': 'rejected',
                    'detail': str(error)[:200],
                    'ansible_rc': None,
                    'started_utc': iso(utc_now()),
                    'finished_utc': iso(utc_now()),
                }
                try:
                    write_json(RESULTS / (build + '.json'), rejected)
                    move_request(request_path, build, 'rejected')
                except OSError:
                    pass
                print('rejected %s: %s' % (build, error), file=sys.stderr)
                continue
            move_request(request_path, build, sha)
            result = deploy(build, sha)
            print('build=%s sha=%s status=%s recovery=%s previous=%s current=%s' % (
                result['build'], result['sha'], result['status'], result['recovery'],
                result['previous_sha'], result['current_sha']))
        return 0


if __name__ == '__main__':
    sys.exit(main())
