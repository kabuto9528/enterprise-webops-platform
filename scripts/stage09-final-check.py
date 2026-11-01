"""Final local safety checks; emits no receiver addresses or credential contents."""
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
sys.path.insert(0, '/home/monitor/webops-stage08/control-libs')
import yaml

def main():
    assert os.geteuid() == 0 and socket.gethostname() == 'monitor01'
    root = Path('/etc/webops-monitoring/alertmanager')
    account = pwd.getpwnam('webops-alertmanager')
    for path, owner, mode in [(root, 0, 0o750), (root/'secrets', 0, 0o750),
                              (root/'alertmanager.yml', 0, 0o640),
                              (root/'secrets/smtp-password', account.pw_uid, 0o400)]:
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode)
        assert info.st_uid == owner and info.st_gid == account.pw_gid
        assert stat.S_IMODE(info.st_mode) == mode
    print('RESTRICTED_CONFIG_AND_SECRET_PERMISSIONS_OK')
    config = yaml.safe_load((root/'alertmanager.yml').read_text())
    defaults = json.loads(Path('/home/monitor/webops-stage09/mail-settings.local.json').read_text())
    assert config['global']['smtp_auth_password_file'] == str(root/'secrets/smtp-password')
    def no_inline_secret(value):
        if isinstance(value, dict):
            assert not any(k in ['smtp_auth_password', 'auth_password'] and v for k,v in value.items())
            for v in value.values(): no_inline_secret(v)
        elif isinstance(value, list):
            for v in value: no_inline_secret(v)
    no_inline_secret(config)
    assert config['global']['smtp_smarthost'] == defaults['smtp']
    assert config['global']['smtp_from'] == defaults['sender']
    assert config['global']['smtp_auth_username'] == defaults['username']
    assert config['route']['receiver'] == 'authorized-external'
    receivers = config['receivers']
    assert len(receivers) == 1 and receivers[0]['name'] == 'authorized-external'
    emails = receivers[0]['email_configs']
    assert len(emails) == 1 and emails[0]['to'] == defaults['recipient'] and emails[0]['send_resolved'] is True
    assert config['global']['smtp_require_tls'] is True
    assert not emails[0]['tls_config'].get('insecure_skip_verify', False)
    print('AUTHORIZED_SINGLE_RECIPIENT_AND_FILE_REFERENCE_ONLY_OK; TLS_VERIFICATION_ENABLED')
    info = Path('/var/lib/webops-stage09/external-test-authorization.json').stat()
    assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
    events = json.loads(Path('/var/lib/webops-stage09/drill-observations.json').read_text())
    assert events[-1]['event'] == 'DRILL_COUNTERS_OK_RECIPIENT_RECEPTION_EVIDENCE_STILL_REQUIRED'
    assert events[-1]['counters'] == {'total': 2.0, 'failed': 0.0}
    print('ROOT_AUTHORIZATION_AND_REAL_DRILL_REPORT_OK')
    for unit in ['webops-alertmanager','webops-prometheus','webops-grafana','webops-blackbox','webops-metrics-tunnel']:
        r = subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True,timeout=10)
        assert r.returncode == 0
        print('SERVICE_ACTIVE='+unit)
    timers = subprocess.check_output(['systemctl','list-timers','--all','--no-pager','--no-legend','webops-stage09-restore-*'],text=True)
    assert not timers.strip()
    print('RESTORE_TIMERS_CLEARED')
    def get(port,path):
        return json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}'+path,timeout=5))
    def query(expr):
        return get(9090,'/api/v1/query?'+urllib.parse.urlencode({'query':expr}))['data']['result']
    up = query('up'); probe=query('probe_success{job="http_health"}')
    assert len(up)==6 and all(float(v['value'][1])==1 for v in up)
    assert len(probe)==1 and float(probe[0]['value'][1])==1
    assert not get(9093,'/api/v2/alerts')
    assert not query('ALERTS{alertstate="firing"}')
    print('SIX_UP_AND_HTTP_PROBE_OK; NO_ACTIVE_ALERTS')
    print('STAGE09_FINAL_LOCAL_CHECKS_OK_RECIPIENT_EVIDENCE_REQUIRED')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('STAGE09_FINAL_LOCAL_CHECKS_FAILED type='+type(error).__name__)
        raise SystemExit(1)
