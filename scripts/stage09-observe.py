"""Read-only observations; deliberately excludes status/config and raw receiver errors."""
import json
import subprocess
import urllib.request
from datetime import datetime, timezone


def get(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
        return response.read().decode()


print('TIME='+datetime.now(timezone.utc).isoformat())
print('ALERTMANAGER_READY='+str(bool(get(9093, '/-/ready'))))
alerts = json.loads(get(9093, '/api/v2/alerts'))
print('ALERTMANAGER_ALERTS='+json.dumps([
    {'alertname': a['labels'].get('alertname'), 'node': a['labels'].get('node'),
     'severity': a['labels'].get('severity'), 'state': a['status']['state'],
     'inhibited': bool(a['status'].get('inhibitedBy')), 'silenced': bool(a['status'].get('silencedBy')),
     'receivers': [r['name'] for r in a.get('receivers', [])]} for a in alerts]))
rules = json.loads(get(9090, '/api/v1/rules?type=alert'))['data']['groups']
print('RULES='+json.dumps([
    {'name': r['name'], 'state': r['state'], 'health': r['health'], 'for': r['duration']}
    for g in rules for r in g['rules']]))
for port in [9090, 9093]:
    metrics = get(port, '/metrics')
    names = ('prometheus_config_last_reload_successful', 'alertmanager_config_last_reload_successful',
             'alertmanager_notifications_total', 'alertmanager_notifications_failed_total')
    print(f'METRICS_{port}='+json.dumps([line for line in metrics.splitlines()
          if not line.startswith('#') and line.startswith(names)]))
flags = json.loads(get(9090, '/api/v1/status/flags'))['data']
print('PROMETHEUS_ADMIN='+flags['web.enable-admin-api']+' LIFECYCLE='+flags['web.enable-lifecycle'])
listeners = subprocess.check_output(['ss', '-H', '-lntu'], text=True)
print('LISTENERS='+json.dumps([line.split()[4] for line in listeners.splitlines()
      if line.split()[4].rsplit(':', 1)[-1] in ['9090', '9093', '9094']]))
print('STAGE09_OBSERVATION_ONLY_NOT_EXTERNAL_RECEPTION_PROOF')
