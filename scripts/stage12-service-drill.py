#!/usr/bin/python3
"""Bounded web01-hz service drill. Default is read-only preflight.

Run only through the existing administrative SSH connection. --execute causes
up to four minutes of real project downtime; use a separate rescue SSH session.
Observe Prometheus/Alertmanager independently; this script cannot prove email
receipt or stage acceptance. No packages or authorization changes are made.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


STATE = Path('/var/lib/webops-release')
DRILL_STATE = Path('/var/lib/webops-stage12')
events = []
report = None


class FaultWindowEnded(Exception):
    """Interrupt a slow in-flight observation at the wall-clock limit."""


def end_fault_window(signum, frame):
    raise FaultWindowEnded()


def record(event, **fields):
    row = dict(time=dt.datetime.now(dt.timezone.utc).isoformat(), event=event, **fields)
    events.append(row)
    print(json.dumps(row), flush=True)
    if report:
        report.write_text(json.dumps(events, indent=2)+'\n')


def command(args, timeout=20, required=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if required and result.returncode:
        # Do not export arbitrary stderr or container environment/configuration.
        raise RuntimeError('command failed: '+args[0]+' rc='+str(result.returncode))
    return result


def endpoint(port, path):
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+str(port)+path, timeout=3) as response:
            data = json.load(response)
            return dict(code=response.status, value=data.get('version', data.get('status')))
    except urllib.error.HTTPError as error:
        return dict(code=error.code)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return dict(code=0)


def snapshot():
    info = json.loads(command(['docker', 'inspect', 'webops-app-1']).stdout)[0]
    return dict(nginx=command(['systemctl', 'is-active', 'nginx'], required=False).stdout.strip(),
                container_status=info['State']['Status'],
                container_health=info['State'].get('Health', {}).get('Status'),
                image_id=info['Image'],
                http_health=endpoint(80, '/healthz'), http_version=endpoint(80, '/version'),
                upstream_health=endpoint(18080, '/healthz'), upstream_version=endpoint(18080, '/version'))


def healthy(sample, current):
    return (sample['nginx'] == 'active' and sample['container_status'] == 'running'
            and sample['container_health'] == 'healthy' and sample['image_id'] == current['image_id']
            and all(sample[k] == dict(code=200, value=current['sha'])
                    for k in ('http_version', 'upstream_version'))
            and all(sample[k] == dict(code=200, value='ok')
                    for k in ('http_health', 'upstream_health')))


def preflight(restore_sha):
    if os.geteuid() != 0 or socket.gethostname() != 'web01-hz':
        raise RuntimeError('Requires authorized root operation on web01-hz only')
    current = json.loads((STATE/'current.json').read_text())
    if not re.fullmatch('[0-9a-f]{40}', current['sha']) or not current.get('known_healthy'):
        raise RuntimeError('Current release is not known healthy')
    compose = Path(current['compose']).resolve(strict=True)
    if not compose.is_relative_to(Path('/opt/webops/releases')) or compose.name != 'compose.json':
        raise RuntimeError('Unexpected project compose path')
    if (Path('/opt/webops/current')/'compose.json').resolve(strict=True) != compose:
        raise RuntimeError('Current symlink and metadata disagree')
    for sha in (current['sha'], restore_sha):
        version = json.loads((STATE/'versions'/(sha+'.json')).read_text())
        image = json.loads(command(['docker', 'image', 'inspect', version['image_id']]).stdout)[0]
        if (not version.get('known_healthy') or version['sha'] != sha
                or image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != sha):
            raise RuntimeError('Recovery image or revision mismatch')
    container = json.loads(command(['docker', 'inspect', 'webops-app-1']).stdout)[0]
    labels = container['Config'].get('Labels', {})
    if labels.get('com.docker.compose.project') != 'webops' or labels.get('com.docker.compose.service') != 'app':
        raise RuntimeError('Container does not belong to project webops/app')
    # Stopping the whole Nginx unit is permitted only with this single project site.
    configs = list(Path('/etc/nginx/conf.d').glob('*.conf'))
    if [p.name for p in configs] != ['webops-app.conf'] or '18080' not in configs[0].read_text():
        raise RuntimeError('Nginx ownership requires review')
    command(['nginx', '-t'])
    expanded = command(['nginx', '-T']).stdout
    expanded = '\n'.join(line.split('#', 1)[0] for line in expanded.splitlines())
    site = '\n'.join(line.split('#', 1)[0] for line in configs[0].read_text().splitlines())
    if len(re.findall(r'\bserver\s*\{', expanded)) != 1 or len(re.findall(r'\bserver\s*\{', site)) != 1:
        raise RuntimeError('Nginx includes another server; do not stop the shared unit')
    sample = snapshot()
    if not healthy(sample, current):
        raise RuntimeError('Baseline is not healthy')
    record('PREFLIGHT_OK', current_sha=current['sha'], recovery_sha=restore_sha,
           compose=str(compose), state=sample)
    return current, compose


def execute(args):
    global report
    # Use the release engine's real lock to exclude CI/manual version switches.
    with (STATE/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current, compose = preflight(args.restore_sha)
        directory = DRILL_STATE
        directory.mkdir(mode=0o700, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')
        report = directory/(stamp+'-'+args.kind+'.json')
        timer = 'webops-stage12-'+args.kind+'-restore-'+stamp.lower()
        restore = (['/usr/bin/systemctl', 'start', 'nginx'] if args.kind == 'nginx' else
                   ['/usr/bin/docker', 'compose', '-p', 'webops', '-f', str(compose),
                    'up', '-d', '--no-build', '--pull', 'never'])
        stop = (['systemctl', 'stop', 'nginx'] if args.kind == 'nginx' else
                ['docker', 'stop', '--time', '10', 'webops-app-1'])
        command(['systemd-run', '--unit='+timer, '--on-active=6m', *restore])
        try:
            command(['systemctl', 'is-active', '--quiet', timer+'.timer'])
            record('RESTORE_TIMER_ACTIVE', unit=timer, seconds=360)
            old_handler = signal.signal(signal.SIGALRM, end_fault_window)
            try:
                # The deadline includes stop and slow in-flight HTTP/CLI reads.
                signal.setitimer(signal.ITIMER_REAL, 240)
                record('INJECTION_REQUEST', command=stop)
                command(stop)
                record('INJECTION_COMPLETED')
                end = time.monotonic()+240
                while True:
                    sample = snapshot()
                    record('FAULT_LOCAL_OBSERVATION', state=sample)
                    # An unrelated failure ends this drill immediately.
                    if (args.kind == 'nginx' and sample['upstream_health']['code'] != 200
                            or args.kind == 'app' and sample['nginx'] != 'active'):
                        raise RuntimeError('Fault expanded beyond intended component')
                    if time.monotonic() >= end:
                        break
                    time.sleep(min(15, max(0, end-time.monotonic())))
            except FaultWindowEnded:
                record('FAULT_OBSERVATION_DEADLINE_REACHED')
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
                record('RESTORE_REQUEST', command=restore)
                command(restore, timeout=45)
                record('RESTORE_COMMAND_COMPLETED')
            for _ in range(18):
                sample = snapshot()
                if healthy(sample, current):
                    record('FINAL_LOCAL_HEALTHY', state=sample)
                    command(['systemctl', 'stop', timer+'.timer'])
                    record('RESTORE_TIMER_CANCELLED', unit=timer)
                    break
                time.sleep(5)
            else:
                raise RuntimeError('Recovery incomplete; independent timer retained')
        except BaseException:
            record('INCOMPLETE_TIMER_RETAINED', unit=timer)
            raise
        record('LOCAL_DRILL_FINISHED_MONITOR_AND_RECEIPTS_REQUIRE_SEPARATE_EVIDENCE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', required=True, choices=['nginx', 'app'])
    parser.add_argument('--restore-sha', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.restore_sha):
        parser.error('Full healthy recovery SHA required')
    os.umask(0o077)
    if args.execute:
        execute(args)
    else:
        preflight(args.restore_sha)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        record('STOPPED_INCOMPLETE', error_type=type(error).__name__)
        sys.exit(1)
