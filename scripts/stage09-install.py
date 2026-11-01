"""Bounded, local-only Alertmanager install. Never reads or prints notification secrets."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request

import yaml

ROOT = Path('/home/monitor/webops-stage09')
CONFIG = Path('/etc/webops-monitoring')
OPT = Path('/opt/webops-monitoring')


def run(argv):
    # Parser errors can contain config values. Never stream stdout/stderr.
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise RuntimeError('COMMAND_FAILED: ' + argv[0] + ' exit=' + str(result.returncode))
    return result.stdout


def main():
    assert os.geteuid() == 0 and socket.gethostname() == 'monitor01'
    os.umask(0o077)
    # Require a healthy baseline before installing or reconciling alerting.
    def query(expression):
        import urllib.parse
        url = 'http://127.0.0.1:9090/api/v1/query?'+urllib.parse.urlencode({'query': expression})
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.load(response)['data']['result']
    baseline = query('up')
    probes = query('probe_success{job="http_health"}')
    assert len(baseline) == 6 and all(float(item['value'][1]) == 1 for item in baseline), 'Existing targets must all be healthy'
    assert len(probes) == 1 and float(probes[0]['value'][1]) == 1, 'Existing HTTP probe must be healthy'
    lock = json.loads((ROOT/'monitoring/alertmanager/lock.json').read_text())
    archive = ROOT/'downloads'/lock['file']
    assert hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() == lock['sha256']
    try:
        pwd.getpwnam('webops-alertmanager')
    except KeyError:
        run(['useradd', '--system', '--no-create-home', '--shell', '/sbin/nologin', 'webops-alertmanager'])
    account = pwd.getpwnam('webops-alertmanager')
    conf = CONFIG/'alertmanager'
    conf.mkdir(mode=0o750, exist_ok=True)
    conf.chmod(0o750)
    os.chown(conf, 0, account.pw_gid)
    secrets = conf/'secrets'
    secrets.mkdir(mode=0o700, exist_ok=True)
    os.chown(secrets, 0, account.pw_gid)
    secrets.chmod(0o750)
    data = Path('/var/lib/webops-monitoring/alertmanager')
    data.mkdir(mode=0o700, exist_ok=True)
    os.chown(data, account.pw_uid, account.pw_gid)
    data.chmod(0o700)
    version = OPT/lock['directory']
    if not all((version/b).is_file() for b in ['alertmanager', 'amtool']):
        with tarfile.open(archive) as package:
            assert all(m.name == lock['directory'] or m.name.startswith(lock['directory']+'/') for m in package.getmembers())
            package.extractall(OPT, filter='data')
    # The restrictive installation umask can leave extracted directories at 0700.
    # The unprivileged service must be able to traverse its root-owned binary directory.
    os.chown(version, 0, 0)
    version.chmod(0o755)
    for binary in ['alertmanager', 'amtool']:
        os.chown(version/binary, 0, 0)
        (version/binary).chmod(0o755)
    amtool = str(version/'amtool')
    promtool = str(OPT/'prometheus-3.13.5.linux-amd64/promtool')
    run([promtool, 'check', 'rules', str(ROOT/'monitoring/prometheus/alerts.yml')])
    run([amtool, 'check-config', str(ROOT/'monitoring/alertmanager/alertmanager.yml')])
    state = Path('/var/lib/webops-stage09')
    state.mkdir(mode=0o700, exist_ok=True)
    backup = state/time.strftime('before-%Y%m%d-%H%M%S')
    backup.mkdir(mode=0o700)
    changed = []

    def install(source, target, group, mode):
        content = source.read_bytes() if isinstance(source, Path) else source
        old = target.read_bytes() if target.exists() else None
        if old != content:
            if old is not None:
                (backup/target.name).write_bytes(old)
            target.write_bytes(content)
            changed.append(str(target))
        os.chown(target, 0, group)
        target.chmod(mode)

    prom = CONFIG/'prometheus/prometheus.yml'
    old_prom = prom.read_bytes()
    config = yaml.safe_load(old_prom)
    expected = {'alertmanagers': [{'static_configs': [{'targets': ['127.0.0.1:9093']}]}]}
    assert not config.get('alerting') or config['alerting'] == expected, 'Existing alertmanager target requires review'
    rules = config.setdefault('rule_files', [])
    rulepath = str(CONFIG/'prometheus/alerts.yml')
    if rulepath not in rules:
        rules.append(rulepath)
    config['alerting'] = expected
    candidate = backup/'prometheus.candidate.yml'
    candidate.write_text(yaml.safe_dump(config, sort_keys=False))
    prom_group = pwd.getpwnam('webops-prometheus').pw_gid
    install(ROOT/'monitoring/prometheus/alerts.yml', Path(rulepath), prom_group, 0o640)
    run([promtool, 'check', 'config', str(candidate)])
    actual_am = conf/'alertmanager.yml'
    if not actual_am.exists():
        install(ROOT/'monitoring/alertmanager/alertmanager.yml', actual_am, account.pw_gid, 0o640)
    run([amtool, 'check-config', str(actual_am)])
    install(candidate, prom, prom_group, 0o640)
    unit = Path('/etc/systemd/system/webops-alertmanager.service')
    install(ROOT/'monitoring/alertmanager/alertmanager.service', unit, 0, 0o644)
    run(['restorecon', '-RF', str(conf), str(data), str(version)])
    run(['systemctl', 'daemon-reload'])
    was_active = subprocess.run(['systemctl', 'is-active', '--quiet', 'webops-alertmanager']).returncode == 0
    if str(unit) in changed or str(actual_am) in changed or not was_active:
        run(['systemctl', 'restart', 'webops-alertmanager'])
    run(['systemctl', 'enable', 'webops-alertmanager'])
    # SIGHUP reloads rules without enabling either HTTP management API.
    if str(prom) in changed or rulepath in changed:
        run(['systemctl', 'kill', '--kill-whom=main', '--signal=HUP', 'webops-prometheus'])
    for endpoint in ['http://127.0.0.1:9093/-/ready', 'http://127.0.0.1:9090/-/ready']:
        for attempt in range(20):
            try:
                with urllib.request.urlopen(endpoint, timeout=3) as response:
                    assert response.status == 200
                break
            except Exception:
                if attempt == 19:
                    raise RuntimeError('READINESS_FAILED') from None
                time.sleep(2)
    print('STAGE09_CHANGED_FILES='+str(len(changed)))
    print('STAGE09_BACKUP='+str(backup))
    print('STAGE09_LOCAL_INSTALL_OK_EXTERNAL_RECEPTION_NOT_ACCEPTED')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No arbitrary exception text from parsers/HTTP libraries.
        print('STAGE09_INSTALL_FAILED type='+type(error).__name__+'; inspect locally; retain backups')
        raise SystemExit(1)
