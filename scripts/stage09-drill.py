"""Real tunnel interruption with independent eight-minute restoration and safe observations.

Requires locally recorded recipient authorization. Successful counters do NOT prove receipt.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def get(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
        return response.read().decode()


def query(expr):
    return json.loads(get(9090, '/api/v1/query?'+urllib.parse.urlencode({'query': expr})))['data']['result']


def counters():
    result = {'total': 0.0, 'failed': 0.0}
    for line in get(9093, '/metrics').splitlines():
        for key, name in [('total', 'alertmanager_notifications_total'), ('failed', 'alertmanager_notifications_failed_total')]:
            if line.startswith(name+'{') or line.startswith(name+' '):
                result[key] += float(line.rsplit(' ', 1)[1])
    return result


def command(argv):
    subprocess.run(argv, check=True, capture_output=True, timeout=30)


def main():
    assert os.geteuid() == 0 and socket.gethostname() == 'monitor01'
    state = Path('/var/lib/webops-stage09')
    authorization = state/'external-test-authorization.json'
    info = authorization.stat()
    assert info.st_uid == 0 and info.st_mode & 0o077 == 0
    auth = json.loads(authorization.read_text())
    assert auth['channel'] == 'email' and auth['recipient']
    assert time.time()-info.st_mtime < 86400, 'Refresh test authorization after one day'
    assert not json.loads(get(9093, '/api/v2/alerts')), 'Review existing alerts before drill'
    values = query('up')
    assert len(values) == 6 and all(float(v['value'][1]) == 1 for v in values)
    probes = query('probe_success{job="http_health"}')
    assert len(probes) == 1 and float(probes[0]['value'][1]) == 1
    command(['systemctl', 'is-active', '--quiet', 'webops-metrics-tunnel'])
    before = counters()
    events = []

    def record(event, **details):
        entry = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event, **details}
        events.append(entry)
        print(json.dumps(entry), flush=True)

    def wait_for(predicate, limit, description):
        deadline = time.monotonic()+limit
        while time.monotonic() < deadline:
            if predicate():
                record(description)
                return
            time.sleep(5)
        raise RuntimeError(description+' timed out')

    def delivered(count):
        now = counters()
        assert now['failed'] == before['failed'], 'Delivery failure counter increased'
        assert now['total'] <= before['total']+count, 'Unexpected repeat/other delivery'
        return now['total'] == before['total']+count

    watchdog = 'webops-stage09-restore-'+str(int(time.time()))
    command(['systemd-run', '--unit='+watchdog, '--on-active=8m', '/usr/bin/systemctl', 'start', 'webops-metrics-tunnel'])
    try:
        command(['systemctl', 'stop', 'webops-metrics-tunnel'])
        record('TUNNEL_STOPPED_WITH_INDEPENDENT_RESTORE_TIMER')

        def routed():
            alerts = json.loads(get(9093, '/api/v2/alerts'))
            return any(a['labels'].get('alertname') == 'NodeExporterDown'
                       and a['labels'].get('node') == 'web01-hz'
                       and a['status']['state'] == 'active'
                       and not a['status'].get('silencedBy') and not a['status'].get('inhibitedBy')
                       and [r['name'] for r in a['receivers']] == ['authorized-external'] for a in alerts)

        wait_for(routed, 230, 'FIRING_ROUTED_TO_AUTHORIZED_EXTERNAL')
        wait_for(lambda: delivered(1), 70, 'ONE_FIRING_DELIVERY_COUNTER_OBSERVED')
        # Cross one group_interval; the configured four-hour repeat is not fully elapsed.
        start = time.monotonic()
        while time.monotonic()-start < 65:
            assert delivered(1)
            time.sleep(5)
        record('NO_DUPLICATE_DURING_65_SECOND_OBSERVATION')
    finally:
        command(['systemctl', 'start', 'webops-metrics-tunnel'])
        record('TUNNEL_START_REQUESTED')
        # Keep the fallback timer until the service and real metrics have recovered.
        try:
            wait_for(lambda: bool(query('up{job="node",node="web01-hz"} == 1')), 90, 'REAL_CLOUD_METRICS_RESTORED')
            command(['systemctl', 'stop', watchdog+'.timer'])
        finally:
            report = state/'drill-observations.json'
            report.write_text(json.dumps(events, indent=2)+'\n')
            report.chmod(0o600)
    wait_for(lambda: not query('ALERTS{alertname="NodeExporterDown",node="web01-hz",alertstate="firing"}'), 60, 'RULE_RECOVERED')
    wait_for(lambda: delivered(2), 90, 'ONE_RECOVERY_DELIVERY_COUNTER_OBSERVED')
    record('DRILL_COUNTERS_OK_RECIPIENT_RECEPTION_EVIDENCE_STILL_REQUIRED', counters=counters())
    report.write_text(json.dumps(events, indent=2)+'\n')
    print('STAGE09_NOT_ACCEPTED_UNTIL_ACTUAL_FIRING_AND_RESOLVED_RECEIPTS_ARE_CONFIRMED')


if __name__ == '__main__':
    import fcntl
    os.umask(0o077)
    lockfile = None
    try:
        assert os.geteuid() == 0 and socket.gethostname() == 'monitor01'
        lockfile = open('/run/webops-stage09-drill.lock', 'w')
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('STAGE09_DRILL_INCOMPLETE type='+type(error).__name__+'; inspect local safe observations and restore timer')
        raise SystemExit(1)
