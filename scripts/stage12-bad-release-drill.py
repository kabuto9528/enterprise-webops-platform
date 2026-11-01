#!/usr/bin/python3
"""Observe a reviewed bad release, then use the installed engine's recovery.

Default: read-only preflight. --execute is an intentional bounded outage.
The installed engine file and its default production timing are never changed.
Only the fixed bad SHA's failed activation is held, in memory, to permit the
existing two-minute alert to be observed. A 240s deadline covers activation and
observation; an independent 360s timer restores the pinned healthy Compose.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BAD = '464dde9e9df83c0b20ac78ec4cd780d8a94a3901'
GOOD = '31cdc9ee0da24099b6a93632b7d4501d4928bae2'
ENGINE = Path('/usr/local/sbin/webops-release')
ENGINE_HASH = '050b13f916108f452a5a2d92fc21a465600309e6d074b177d745968e0aceb8ec'
ARCHIVE = Path('/var/lib/webops-release/incoming')/(BAD+'.tar')
ARCHIVE_HASH = '1f5274632c32eb11c64dcc45700ca0c94122245919e1516312ad676ffeb24a0b'
STATE = Path('/var/lib/webops-release')
OUT = sys.stdout
events = []
report = None
timer = None
activation_seen = False


class ObservationDeadline(Exception):
    pass


def deadline(signum, frame):
    raise ObservationDeadline()


def record(event, **fields):
    row = dict(time=dt.datetime.now(dt.timezone.utc).isoformat(), event=event, **fields)
    events.append(row)
    print(json.dumps(row), file=OUT, flush=True)
    if report:
        temp = report.with_suffix('.tmp')
        temp.write_text(json.dumps(events, indent=2)+'\n')
        os.replace(temp, report)


def command(args, timeout=20):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError('Command failed: '+args[0]+' rc='+str(p.returncode))
    return p.stdout


def response(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:'+str(port)+path, timeout=3) as r:
            return dict(code=r.status, body=json.load(r))
    except urllib.error.HTTPError as error:
        return dict(code=error.code)


def bad_state():
    item = json.loads(command(['docker', 'inspect', 'webops-app-1']))[0]
    result = dict(image=item['Image'], state=item['State']['Status'],
                  revision=item['Config'].get('Labels', {}).get('org.opencontainers.image.revision'),
                  nginx=command(['systemctl', 'is-active', 'nginx']).strip(),
                  health=response(80, '/healthz'), version=response(80, '/version'))
    if (result['revision'] != BAD or result['state'] != 'running' or result['nginx'] != 'active'
            or result['health']['code'] != 503
            or result['version'] != dict(code=200, body={'version': BAD})):
        raise RuntimeError('Actual bad runtime differs from the reviewed fault; recover now')
    return result


def activation(current, original, data):
    global timer, activation_seen
    if data['sha'] != BAD:
        # The original main() calls this branch for its actual healthy recovery.
        return original(data)
    latest = json.loads((STATE/'current.json').read_text())
    if latest['sha'] != current['sha'] or latest['image_id'] != current['image_id']:
        raise RuntimeError('Recovery baseline changed before activation; do not switch')
    restore = ['/usr/bin/docker', 'compose', '-p', 'webops', '-f', current['compose'],
               'up', '-d', '--no-build', '--pull', 'never']
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dt%H%M%S')
    timer = 'webops-stage12-bad-restore-'+stamp
    command(['systemd-run', '--unit='+timer, '--on-active=6m', *restore])
    command(['systemctl', 'is-active', '--quiet', timer+'.timer'])
    record('INDEPENDENT_RESTORE_TIMER_ACTIVE', unit=timer, seconds=360)
    old = signal.signal(signal.SIGALRM, deadline)
    failure = None
    try:
        signal.setitimer(signal.ITIMER_REAL, 240)
        record('ORIGINAL_ENGINE_BAD_ACTIVATION_REQUEST', sha=BAD)
        activation_seen = True
        try:
            original(data)
        except ObservationDeadline:
            raise
        except Exception as error:
            failure = error
            record('ORIGINAL_ENGINE_ACTIVATION_FAILED', error_type=type(error).__name__)
        else:
            raise RuntimeError('Bad candidate unexpectedly passed; recover without holding')
        # Do not modify health checks or mark the bad version known_healthy.
        while True:
            record('BOUNDED_BAD_RUNTIME_OBSERVATION', state=bad_state())
            time.sleep(15)
    except ObservationDeadline:
        record('ACTIVATION_OBSERVATION_240S_LIMIT_RECOVER_NOW')
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    raise failure or RuntimeError('Bad activation observation deadline reached')


def preflight():
    if os.geteuid() != 0 or socket.gethostname() != 'web01-hz':
        raise RuntimeError('Requires authorized root operation on web01-hz')
    if hashlib.sha256(ENGINE.read_bytes()).hexdigest() != ENGINE_HASH:
        raise RuntimeError('Installed engine differs from the reviewed source')
    if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != ARCHIVE_HASH:
        raise RuntimeError('Reviewed Git archive differs')
    loader = importlib.machinery.SourceFileLoader('stage12_installed_release', str(ENGINE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    engine = importlib.util.module_from_spec(spec)
    previous_no_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(engine)
    finally:
        sys.dont_write_bytecode = previous_no_bytecode
    current = json.loads((STATE/'current.json').read_text())
    if current['sha'] != GOOD or (STATE/'versions'/(BAD+'.json')).exists():
        raise RuntimeError('Current or bad-version metadata requires review')
    saved = engine.read_version(GOOD)
    engine.health(saved)
    if current['image_id'] != saved['image_id'] or current['compose'] != saved['compose']:
        raise RuntimeError('Current and saved recovery metadata differ')
    path = Path(current['compose']).resolve(strict=True)
    if not path.is_relative_to(Path('/opt/webops/releases')) or path.name != 'compose.json':
        raise RuntimeError('Unexpected recovery Compose path')
    record('READ_ONLY_PREFLIGHT_OK', good_sha=GOOD, bad_sha=BAD,
           engine_sha256=ENGINE_HASH, archive_sha256=ARCHIVE_HASH)
    return engine, current


def main():
    global report
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    engine, current = preflight()
    if not args.execute:
        return 0
    root = Path('/var/lib/webops-stage12')
    root.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')
    report = root/(stamp+'-bad-release.json')
    original = engine.activate
    engine.activate = lambda data: activation(current, original, data)
    argv = sys.argv[:]
    sys.argv = [str(ENGINE), 'deploy', BAD, '--archive', str(ARCHIVE), '--archive-sha256', ARCHIVE_HASH]
    code = None
    try:
        with (root/(stamp+'-bad-release-private.log')).open('x') as private:
            with contextlib.redirect_stdout(private), contextlib.redirect_stderr(private):
                code = engine.main()
        record('ORIGINAL_ENGINE_RETURNED', rc=code)
    finally:
        sys.argv = argv
        # If interrupted, still attempt the exact saved healthy runtime.
        if activation_seen:
            try:
                engine.health(current)
            except Exception:
                record('DIRECT_RESCUE_REQUEST')
                command(['docker', 'compose', '-p', 'webops', '-f', current['compose'],
                         'up', '-d', '--no-build', '--pull', 'never'], timeout=45)
                engine.health(current)
                record('DIRECT_RESCUE_HEALTHY_ENGINE_RESULT_REQUIRES_REVIEW')
            record('FINAL_HEALTHY_RUNTIME_VERIFIED', sha=GOOD)
        else:
            record('NO_ACTIVATION_REQUESTED_NO_DIRECT_RESCUE_NEEDED')
        if timer:
            command(['systemctl', 'stop', timer+'.timer'])
            record('INDEPENDENT_RESTORE_TIMER_CANCELLED', unit=timer)
    candidates = sorted((STATE/'attempts').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:5]
    attempt = next((json.loads(p.read_text()) for p in candidates
                    if json.loads(p.read_text()).get('sha') == BAD
                    and p.name >= stamp), None)
    if code != 1 or not activation_seen or not attempt or attempt.get('recovery') != 'succeeded':
        raise RuntimeError('Actual failed-release/recovery evidence incomplete')
    record('ORIGINAL_FAILED_RELEASE_AND_RECOVERY_VERIFIED',
           attempt={k:attempt.get(k) for k in ['sha','previous','status','recovery','started_utc','finished_utc']})
    record('REAL_BAD_RELEASE_RECOVERED_MONITOR_AND_RECEIPTS_REQUIRE_SEPARATE_EVIDENCE')
    return code  # The genuine release remains nonzero even after recovery.


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as error:
        record('INCOMPLETE', error_type=type(error).__name__)
        sys.exit(1)
