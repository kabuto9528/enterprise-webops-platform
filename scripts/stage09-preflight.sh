#!/usr/bin/env bash
set -eu
whoami
hostname
cat /etc/os-release
python3 --version
git --version
test "$(hostname)" = monitor01
printf 'TIME='; date -Iseconds
free -m
getenforce
for unit in webops-prometheus webops-grafana webops-blackbox webops-metrics-tunnel; do
  printf '%s=' "$unit"; systemctl is-active "$unit"
done
printf 'LISTENERS\n'
ss -H -lnt | awk '$4 ~ /:(9090|3000|9115|19100|9093|9094)$/ {print $4}'
python3 - <<'PY'
import json, urllib.request, urllib.parse
queries = {
 'up': 'up', 'probe_success': 'probe_success{job="http_health"}',
 'memory_available_ratio': 'node_memory_MemAvailable_bytes{job="node"}/node_memory_MemTotal_bytes{job="node"}',
 'filesystem_available_ratio': 'node_filesystem_avail_bytes{job="node",mountpoint="/"}/node_filesystem_size_bytes{job="node",mountpoint="/"}',
}
for label, expr in queries.items():
    data=json.load(urllib.request.urlopen('http://127.0.0.1:9090/api/v1/query?'+urllib.parse.urlencode({'query':expr}), timeout=5))
    print(label, [(r['metric'].get('node',r['metric'].get('instance')),r['metric'].get('job'),r['value'][1]) for r in data['data']['result']])
try:
    import subprocess
    result=subprocess.run(['sudo','-n','true'],capture_output=True)
    print('SUDO_NONINTERACTIVE_AVAILABLE='+str(result.returncode==0))
except OSError:
    print('SUDO_NONINTERACTIVE_AVAILABLE=False')
PY
