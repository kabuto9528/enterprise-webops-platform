#!/usr/bin/python3
"""Bounded, read-only monitoring observations without credentials/configuration."""
import argparse
import datetime as dt
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.parse
import urllib.request


def get(port, path):
    with urllib.request.urlopen('http://127.0.0.1:'+str(port)+path, timeout=5) as response:
        return response.read().decode()


def query(expr):
    data = json.loads(get(9090, '/api/v1/query?'+urllib.parse.urlencode({'query': expr})))
    if data['status'] != 'success':
        raise RuntimeError('Prometheus query failed')
    return [dict(job=v['metric'].get('job'), node=v['metric'].get('node'),
                 alert=v['metric'].get('alertname'), state=v['metric'].get('alertstate'),
                 timestamp=v['value'][0], value=v['value'][1]) for v in data['data']['result']]


def snapshot():
    sample = dict(time=dt.datetime.now(dt.timezone.utc).isoformat(),
                  up=query('up'), probe=query('probe_success{job="http_health"}'),
                  rules=query('ALERTS'))
    alerts = json.loads(get(9093, '/api/v2/alerts'))
    sample['alerts'] = [dict(name=a['labels'].get('alertname'), node=a['labels'].get('node'),
                            state=a['status']['state'],
                            silenced=bool(a['status'].get('silencedBy')),
                            inhibited=bool(a['status'].get('inhibitedBy')),
                            receivers=[r['name'] for r in a.get('receivers', [])]) for a in alerts]
    metrics = get(9093, '/metrics').splitlines()
    sample['notifications'] = {}
    for key, name in [('total', 'alertmanager_notifications_total'),
                      ('failed', 'alertmanager_notifications_failed_total')]:
        sample['notifications'][key] = sum(float(line.rsplit(' ', 1)[1]) for line in metrics
                                          if line.startswith(name+'{') or line.startswith(name+' '))
    started = time.monotonic()
    try:
        # Read fully; no head/early pipe closure mistaken for exporter failure.
        text = get(19100, '/metrics')
        sample['direct_tunnel'] = dict(ok='node_uname_info{' in text,
                                       duration_seconds=round(time.monotonic()-started, 3))
    except Exception as error:
        sample['direct_tunnel'] = dict(ok=False, error_type=type(error).__name__,
                                       duration_seconds=round(time.monotonic()-started, 3))
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    sample['monitor_available_mib'] = int(memory['MemAvailable'].split()[0])//1024
    units = ['webops-prometheus', 'webops-alertmanager', 'webops-metrics-tunnel']
    sample['services'] = {u: subprocess.run(['systemctl', 'is-active', u], capture_output=True,
                                           text=True, timeout=5).stdout.strip() for u in units}
    return sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--interval', type=int, default=15)
    args = parser.parse_args()
    if socket.gethostname() != 'monitor01' or not 1 <= args.count <= 20 or not 1 <= args.interval <= 30:
        parser.error('Requires monitor01, count1..20 and interval1..30')
    for number in range(args.count):
        try:
            print(json.dumps(snapshot()), flush=True)
        except Exception as error:
            print(json.dumps(dict(time=dt.datetime.now(dt.timezone.utc).isoformat(),
                                  observation_failed=type(error).__name__)), flush=True)
            return 1
        if number+1 < args.count:
            time.sleep(args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
