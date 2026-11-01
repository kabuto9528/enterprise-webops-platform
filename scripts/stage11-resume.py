#!/usr/bin/python3
"""Operator-run Stage11 repair: backup, install reviewed code, healthy build,
then enable SCM polling and export read-only evidence for at most 45 minutes.
No SSH/firewall/Tailscale/sudo policy or Jenkins security setting is changed.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

STAGE = Path('/home/ops/webops-stage11')
CANDIDATE = STAGE / 'candidate'
BASE = Path('/var/lib/webops-ci')
JOB = Path('/var/lib/jenkins/jobs/webops-ci')
FLAG = Path('/etc/webops/ci/polling-enabled')
REQUEST = BASE / 'request-build'
SESSION = BASE / 'status' / 'acceptance-session.json'
FILES = ['scripts/webops-ci-release.py', 'scripts/webops-git-readonly.py',
         'scripts/stage11-accept.py', 'scripts/stage11-resume.py',
         'jenkins/ci-executor.service', 'jenkins/git-readonly.service',
         'jenkins/ci-bootstrap.groovy', 'Jenkinsfile', 'scripts/ci-pipeline.sh', 'ansible/deploy.yml']


def completed_build(number):
    try:
        root = ET.parse(JOB / 'builds' / str(number) / 'build.xml').getroot()
        result = root.findtext('result')
        log = (JOB / 'builds' / str(number) / 'log').read_text(errors='replace')
        if (result in {'SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'}
                and root.findtext('completed') != 'false'
                and re.search(r'Finished: ' + result + r'\s*$', log)):
            return result
    except (OSError, ET.ParseError):
        pass
    return None


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this script locally with sudo.')
    os.umask(0o027)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = BASE / 'backups' / stamp
    backup.mkdir(parents=True, mode=0o700)
    log = (backup / 'install.log').open('w')
    session = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'phase': 'preflight', 'backup': str(backup), 'samples': 0,
               'guest_available_min_MiB': None, 'swap_used_max_MiB': 0,
               'jenkins_oom_kill_max': 0}

    def publish():
        temporary = SESSION.with_suffix('.tmp')
        temporary.write_text(json.dumps(session, indent=2) + '\n')
        os.chmod(temporary, 0o644)
        os.replace(temporary, SESSION)

    def sample():
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            values[key] = int(value.strip().split()[0])
        available = values['MemAvailable'] // 1024
        session['samples'] += 1
        session['guest_available_min_MiB'] = min(available, session['guest_available_min_MiB'] or available)
        session['swap_used_max_MiB'] = max(session['swap_used_max_MiB'], (values['SwapTotal'] - values['SwapFree']) // 1024)
        events = Path('/sys/fs/cgroup/system.slice/jenkins.service/memory.events')
        if events.exists():
            counters = dict(line.split() for line in events.read_text().splitlines())
            session['jenkins_oom_kill_max'] = max(session['jenkins_oom_kill_max'], int(counters.get('oom_kill', 0)))
        session['last_sample_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()

    def run(args, label, timeout=60):
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        log.write(label + '\n' + result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace') + '\n')
        log.flush()
        if result.returncode:
            raise RuntimeError(label + ' failed; inspect root-private install.log')
        return result.stdout.decode().strip()

    def install(source, target, mode):
        path = Path(target)
        if path.exists():
            shutil.copy2(path, backup / path.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CANDIDATE / source, path)
        os.chown(path, 0, 0)
        os.chmod(path, mode)

    def snapshot():
        run(['/usr/bin/python3', '-B', '/usr/local/libexec/webops-stage11-accept.py'], 'collect evidence')

    def wait_build(number):
        waiting_since = time.monotonic()
        deadline = time.monotonic() + 6000
        while time.monotonic() < deadline:
            sample()
            publish()
            if (JOB / 'builds' / str(number) / 'build.xml').exists():
                REQUEST.unlink(missing_ok=True)
            elif time.monotonic() - waiting_since > 180:
                raise RuntimeError('build was not scheduled within 180s; inspect Jenkins STAGE11_BOOTSTRAP_ERROR')
            result = completed_build(number)
            if result:
                snapshot()
                return result
            time.sleep(5)
        raise RuntimeError('build wait timed out; verify target lock/attempt before retrying')

    try:
        # Diagnose prior failure without exporting raw internal logs or secrets.
        try:
            previous = json.loads((BASE / 'status' / 'last-release.json').read_text())
            build, revision = str(previous.get('build', '')), str(previous.get('sha', ''))
            if re.fullmatch(r'[0-9]{1,7}', build) and re.fullmatch(r'[0-9a-f]{40}', revision):
                text = (BASE / 'logs' / (build + '-' + revision + '.log')).read_text(errors='replace')
                session['previous_failure_diagnostic'] = {
                    'build': build, 'temp_creation_failure': 'Failed to create temporary directory' in text,
                    'read_only_filesystem': 'Read-only file system' in text,
                    'root_home_tmp': '/root/.ansible/tmp' in text,
                    'at_controller_export_task': 'TASK [Create an isolated controller export directory]' in text,
                    'ssh_timeout': 'Connection timed out' in text,
                    'permission_denied': 'Permission denied' in text,
                }
        except (OSError, ValueError):
            pass
        sha = (STAGE / 'candidate.sha').read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise RuntimeError('invalid candidate commit')
        head = run(['git', '--git-dir=/srv/git/webops.git', 'rev-parse', 'refs/heads/main'], 'read bare head')
        if head != sha:
            raise RuntimeError('candidate is no longer the bare main head')
        for name in FILES:
            committed = subprocess.check_output(['git', '--git-dir=/srv/git/webops.git', 'show', sha + ':' + name])
            if committed != (CANDIDATE / name).read_bytes():
                raise RuntimeError('uploaded file differs from reviewed commit: ' + name)
        session['candidate_sha'] = sha
        run(['systemctl', 'is-active', 'jenkins'], 'Jenkins preflight')
        # No active executor or incomplete Pipeline may be interrupted by install.
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'webops-ci-release.service']).returncode == 0
        builds = sorted((int(p.name) for p in (JOB / 'builds').iterdir() if p.name.isdigit()), reverse=True)
        if active or (builds and not completed_build(builds[0])):
            raise RuntimeError('a build/executor is still active; leave it running')
        for path in (JOB / 'config.xml', FLAG):
            if path.exists():
                shutil.copy2(path, backup / path.name)
        run(['systemctl', 'stop', 'webops-ci-release.path'], 'pause request watcher')
        if subprocess.run(['id', 'webops-git-ro'], capture_output=True).returncode:
            run(['useradd', '--system', '--no-create-home', '--shell', '/sbin/nologin',
                 '--gid', 'webops-code', 'webops-git-ro'], 'create Git read-only service user')
        install('scripts/webops-git-readonly.py', '/usr/local/libexec/webops-git-readonly.py', 0o755)
        install('scripts/webops-ci-release.py', '/usr/local/sbin/webops-ci-release', 0o750)
        install('scripts/stage11-accept.py', '/usr/local/libexec/webops-stage11-accept.py', 0o755)
        install('jenkins/git-readonly.service', '/etc/systemd/system/webops-git-readonly.service', 0o644)
        install('jenkins/ci-executor.service', '/etc/systemd/system/webops-ci-release.service', 0o644)
        install('jenkins/ci-bootstrap.groovy', '/var/lib/jenkins/init.groovy.d/stage11-bootstrap.groovy', 0o644)
        run(['systemctl', 'daemon-reload'], 'reload fixed units')
        run(['systemctl', 'enable', '--now', 'webops-git-readonly.service'], 'enable loopback read-only Git')
        run(['systemctl', 'restart', 'webops-git-readonly.service'], 'load reviewed read-only Git code')
        for attempt in range(20):
            ready = subprocess.run(['git', 'ls-remote', 'http://127.0.0.1:19418/webops.git', 'refs/heads/main'],
                                   capture_output=True, timeout=15)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('read-only Git endpoint did not become ready')
        run(['systemctl', 'start', 'webops-ci-release.path'], 'resume request watcher')
        # Disable polling until a real manual healthy build has completed.
        FLAG.unlink(missing_ok=True)
        next_build = int((JOB / 'nextBuildNumber').read_text().strip())
        session.update(phase='manual_build', manual_build=next_build)
        REQUEST.write_text('operator-requested\n')
        os.chmod(REQUEST, 0o644)
        print('STAGE11_MANUAL_BUILD_STARTING=' + str(next_build), flush=True)
        run(['systemctl', 'restart', 'jenkins'], 'restart for reviewed bootstrap', timeout=180)
        result = wait_build(next_build)
        REQUEST.unlink(missing_ok=True)
        session['manual_result'] = result
        if result != 'SUCCESS':
            raise RuntimeError('manual healthy pipeline failed; polling remains disabled')
        FLAG.touch(mode=0o644)
        session['phase'] = 'enable_polling'
        publish()
        run(['systemctl', 'restart', 'jenkins'], 'enable SCM polling after healthy build', timeout=180)
        session['phase'] = 'polling_observer'
        snapshot()
        print('STAGE11_MANUAL_SUCCESS=' + str(next_build), flush=True)
        print('STAGE11_POLLING_ENABLED=1; keep this terminal open while push acceptance runs.', flush=True)
        deadline = time.monotonic() + 2700
        last = None
        while time.monotonic() < deadline and not (STAGE / 'acceptance-observer.stop').exists():
            sample()
            publish()
            numbers = sorted(int(p.name) for p in (JOB / 'builds').iterdir() if p.name.isdigit())
            latest = numbers[-1] if numbers else 0
            marker = (latest, completed_build(latest))
            if marker != last:
                snapshot()
                last = marker
            time.sleep(5)
        snapshot()
        session['phase'] = 'observer_finished'
        publish()
        print('STAGE11_EVIDENCE_READY=1', flush=True)
    except Exception as error:
        REQUEST.unlink(missing_ok=True)
        session.update(phase='failed', detail=str(error))
        publish()
        if Path('/usr/local/libexec/webops-stage11-accept.py').exists():
            snapshot()
        print('STAGE11_RESUME_FAILED=' + str(error), flush=True)
        raise SystemExit(1)
    finally:
        log.close()


if __name__ == '__main__':
    main()
