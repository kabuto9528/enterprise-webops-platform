#!/usr/bin/env python3
"""Run as root on monitor01. Never output Grafana credentials or real targets."""
import base64
import json
import math
from pathlib import Path
import subprocess
import time
import sys
import urllib.parse
import urllib.request

assert subprocess.check_output(['hostname'], text=True).strip() == 'monitor01'
component_check = sys.argv[1:] == ['--component-check']
ROOT = Path('/etc/webops-monitoring')
DATA = Path('/var/lib/webops-stage08')
DATA.mkdir(mode=0o700, exist_ok=True)
password = (ROOT / 'grafana/admin-password').read_text().strip()
assert len(password) >= 40
auth = 'Basic ' + base64.b64encode(('webops-admin:' + password).encode()).decode()


def request(base, path, payload=None, authenticated=False):
    headers = {'Authorization': auth} if authenticated else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def prom(query, **kw):
    result = request('http://127.0.0.1:9090', '/api/v1/query?' + urllib.parse.urlencode({'query': query, **kw}))
    assert result['status'] == 'success'
    return result['data']['result']


def graf(path, payload=None):
    return request('http://127.0.0.1:3000', path, payload, True)


def ready():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:9090/-/ready', timeout=3) as r:
                assert r.status == 200
            assert request('http://127.0.0.1:3000', '/api/health')['database'] == 'ok'
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError('readiness timeout')


ready()
print('READINESS_AND_GRAFANA_DATABASE_OK', flush=True)
for _ in range(24):
    up = prom('up')
    probe = prom('probe_success{job="http_health"}')
    cpu = prom('rate(node_cpu_seconds_total{job="node",mode="idle"}[5m])')
    history = prom('count_over_time(up[2m])')
    expected = {('node', n) for n in ('ops01', 'web01-hz', 'monitor01')} | {
        ('prometheus', 'monitor01'), ('blackbox', 'monitor01'), ('http_health', 'web01-hz-http')}
    if ({(r['metric']['job'], r['metric']['instance']) for r in up} == expected
            and (component_check or all(r['value'][1] == '1' for r in up))
            and len(probe) == 1 and (component_check or probe[0]['value'][1] == '1') and cpu
            and len(history) == 6 and all(float(r['value'][1]) >= 3 for r in history)):
        break
    time.sleep(10)
else:
    raise RuntimeError('expected live targets/probe/CPU samples missing')
print('UP=' + json.dumps(up), flush=True)
print('PROBE_SUCCESS=' + json.dumps(probe), flush=True)
identity = prom('node_uname_info{job="node"}')
assert len(identity) >= (2 if component_check else 3)
assert all(r['metric']['node'] == r['metric']['nodename'] for r in identity)
print('OBSERVED_NODE_IDENTITIES=' + str(len(identity)), flush=True)

dashboard = graf('/api/dashboards/uid/webops-overview')['dashboard']
assert len(dashboard['panels']) == 7
for panel in dashboard['panels']:
    expr = panel['targets'][0]['expr']
    result = graf('/api/ds/query', {'from': 'now-5m', 'to': 'now', 'queries': [{
        'refId': 'A', 'datasource': {'type': 'prometheus', 'uid': 'webops-prometheus'},
        'expr': expr, 'range': True, 'instant': False, 'intervalMs': 30000,
        'maxDataPoints': 1000}]})['results']['A']
    assert not result.get('error'), panel['title']
    frames = result.get('frames', [])
    values = [x for f in frames for field, column in zip(f['schema']['fields'], f['data']['values'])
              if field['type'] == 'number' for x in column if isinstance(x, (int, float)) and math.isfinite(x)]
    assert len(values) >= 2, panel['title']
    print('GRAFANA_PANEL_REAL_SERIES=' + json.dumps({'title': panel['title'], 'frames': len(frames),
          'samples': len(values), 'last_value': values[-1]}, ensure_ascii=False), flush=True)

# Preserve a fixed historical range, not a fresh post-restart sample.
fixed = int(time.time()) - 10
queries = ('up[2m]', 'probe_success{job="http_health"}[2m]')
before = {q: prom(q, time=fixed) for q in queries}
assert all(before.values())
assert all(len(series['values']) >= 2 for series in before[queries[0]])
(DATA / 'historical-before.json').write_text(json.dumps({'time': fixed, 'queries': before}))
dashboard_before = (ROOT / 'grafana/dashboards/webops-overview.json').read_bytes()
db = Path('/var/lib/webops-monitoring/grafana/grafana.db')
assert db.is_file() and db.stat().st_size > 0
prefs_before = graf('/api/user/preferences')
print('RESTARTING_THREE_STAGE08_SERVICES', flush=True)
subprocess.run(['systemctl', 'restart', 'webops-prometheus', 'webops-blackbox', 'webops-grafana'], check=True)
ready()
after = {q: prom(q, time=fixed) for q in queries}
assert before == after, 'fixed historical samples changed across restart'
assert dashboard_before == (ROOT / 'grafana/dashboards/webops-overview.json').read_bytes()
assert graf('/api/dashboards/uid/webops-overview')['dashboard']['uid'] == 'webops-overview'
assert graf('/api/user/preferences') == prefs_before
assert graf('/api/datasources/uid/webops-prometheus')['url'] == 'http://127.0.0.1:9090'
print('HISTORICAL_SAMPLES_DASHBOARD_DATABASE_PERSISTENCE_OK', flush=True)

flags = request('http://127.0.0.1:9090', '/api/v1/status/flags')['data']
assert flags['web.enable-admin-api'] == 'false'
assert flags['web.enable-lifecycle'] == 'false'
# Prometheus normalizes the configured 7d duration to 1w in its flags API.
assert flags['storage.tsdb.retention.time'] in ('7d', '1w', '168h')
listeners = subprocess.check_output(['ss', '-H', '-lnt'], text=True).splitlines()
for port in (9090, 3000, 9115):
    found = [line.split()[3] for line in listeners if line.split()[3].endswith(':' + str(port))]
    assert found == ['127.0.0.1:' + str(port)], found
    print('LOOPBACK_ONLY=' + str(port))
for service in ('prometheus', 'blackbox', 'grafana'):
    subprocess.run(['systemctl', 'is-active', 'webops-' + service], check=True)
    subprocess.run(['systemctl', 'is-enabled', 'webops-' + service], check=True)
    subprocess.run(['systemctl', 'show', 'webops-' + service, '-p', 'User', '-p', 'MemoryCurrent',
                    '-p', 'MemoryMax', '-p', 'NoNewPrivileges', '-p', 'NRestarts'], check=True)
assert subprocess.check_output(['getenforce'], text=True).strip() == 'Enforcing'
print('PROMETHEUS_ADMIN_API_DISABLED_SELINUX_ENFORCING')
if component_check:
    print('STAGE08_COMPONENT_CHECK_OK_FULL_STAGE_NOT_ACCEPTED', flush=True)
else:
    print('STAGE08_ACCEPTANCE_FAILURES=0', flush=True)
