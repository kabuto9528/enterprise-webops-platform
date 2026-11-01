"""Run the stage's exact checks, emitting only allowlisted observations."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

sys.path.insert(0, '/home/monitor/webops-stage08/control-libs')
import yaml

assert os.geteuid() == 0 and socket.gethostname() == 'monitor01'
config = '/etc/webops-monitoring/alertmanager/alertmanager.yml'
receiver = yaml.safe_load(Path(config).read_text())['route']['receiver']
assert receiver in ['unconfigured', 'authorized-external']
commands = [
    ('PROMTOOL_CHECK_RULES', ['/opt/webops-monitoring/prometheus-3.13.5.linux-amd64/promtool', 'check', 'rules', '/etc/webops-monitoring/prometheus/alerts.yml']),
    ('PROMTOOL_TEST_RULES', ['/opt/webops-monitoring/prometheus-3.13.5.linux-amd64/promtool', 'test', 'rules', '/home/monitor/webops-stage09/monitoring/prometheus/alerts.test.yml']),
    ('AMTOOL_CHECK_CONFIG', ['/opt/webops-monitoring/alertmanager-0.34.2.linux-amd64/amtool', 'check-config', config]),
    ('AMTOOL_ROUTE_TEST', ['/opt/webops-monitoring/alertmanager-0.34.2.linux-amd64/amtool', 'config', 'routes', 'test', '--config.file='+config, '--verify.receivers='+receiver, 'alertname=NodeExporterDown', 'node=web01-hz', 'severity=critical', 'category=node-reachability']),
    ('CURL_READY', ['curl', '-f', '--max-time', '5', 'http://127.0.0.1:9093/-/ready']),
    ('CURL_ALERTS', ['curl', '-f', '--max-time', '5', 'http://127.0.0.1:9093/api/v2/alerts']),
]
for label, command in commands:
    result = subprocess.run(command, capture_output=True, timeout=60)
    print(label+'_EXIT='+str(result.returncode), flush=True)
    if result.returncode:
        raise SystemExit(1)
    if label == 'CURL_ALERTS':
        alerts = json.loads(result.stdout)
        print('ACTIVE_ALERT_LABELS='+json.dumps([{'alertname': a['labels'].get('alertname'),
              'node': a['labels'].get('node'), 'state': a['status']['state']} for a in alerts]))
subprocess.run([sys.executable, '/home/monitor/webops-stage09/scripts/stage09-observe.py'], check=True)
print('STAGE09_LOCAL_CHECKS_OK_EXTERNAL_RECEIPT_STILL_REQUIRED')
