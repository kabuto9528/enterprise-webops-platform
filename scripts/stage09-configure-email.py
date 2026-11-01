"""Interactive monitor01-only setup. Password is entered without echo on the local terminal."""
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, '/home/monitor/webops-stage08/control-libs')
import yaml

STEP = 'preflight'


def step(name):
    global STEP
    STEP = name
    print('STAGE09_EMAIL_STEP='+name, flush=True)


def main():
    step('preflight')
    assert os.geteuid() == 0 and socket.gethostname() == 'monitor01' and sys.stdin.isatty()
    os.umask(0o077)
    root = Path('/etc/webops-monitoring/alertmanager')
    assert (root/'alertmanager.yml').exists(), 'Install preparation configuration first'
    with urllib.request.urlopen('http://127.0.0.1:9093/api/v2/alerts', timeout=5) as response:
        assert not json.load(response), 'Existing alerts must be reviewed before enabling this receiver'
    settings = Path('/home/monitor/webops-stage09/mail-settings.local.json')
    step('approved_settings')
    # These values were explicitly selected and authorized by the user and synced
    # privately. Avoid echoing a password accidentally entered into a text prompt.
    assert settings.is_file(), 'Approved local mail settings are required'
    defaults = json.loads(settings.read_text())
    host = defaults['smtp']
    sender = defaults['sender']
    username = defaults['username']
    recipient = defaults['recipient']
    assert re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{2,5}', host)
    assert 0 < int(host.rsplit(':', 1)[1]) <= 65535
    for address in [sender, recipient]:
        assert re.fullmatch(r'[^\s<>,;@]+@[^\s<>,;@]+\.[^\s<>,;@]+', address)
    assert username and '\n' not in username and '\r' not in username
    print('Using the previously approved local SMTP, sender and ONE recipient settings.')
    print('Only enter the SMTP authorization code at the hidden prompt below.')
    step('hidden_input')
    for attempt in range(3):
        secret = getpass.getpass('SMTP password/app authorization code (hidden, never logged): ')
        if not secret:
            print('STAGE09_EMAIL_INPUT_EMPTY; type the authorization code before pressing Enter', flush=True)
        elif any(ord(character) < 32 or ord(character) == 127 for character in secret):
            print('STAGE09_EMAIL_INPUT_CONTROL_CHARACTER; retry without terminal control keys', flush=True)
        else:
            break
    else:
        raise ValueError('Hidden input rejected')
    step('build_candidate')
    config = yaml.safe_load(Path('/home/monitor/webops-stage09/monitoring/alertmanager/email.example.yml').read_text())
    config['global'].update(smtp_smarthost=host, smtp_from=sender, smtp_auth_username=username)
    config['receivers'][0]['email_configs'][0]['to'] = recipient
    config['receivers'][0]['email_configs'][0]['force_implicit_tls'] = host.endswith(':465')
    email = config['receivers'][0]['email_configs'][0]
    email['tls_config'] = {'min_version': 'TLS12'}
    # Apply a tested compatibility limit only to the same locally selected server.
    if host == defaults.get('smtp') and defaults.get('tls_max_version'):
        assert defaults['tls_max_version'] in ['TLS12', 'TLS13']
        email['tls_config']['max_version'] = defaults['tls_max_version']
    state = Path('/var/lib/webops-stage09')
    step('backup')
    backup = state/time.strftime('receiver-before-%Y%m%d-%H%M%S')
    backup.mkdir(mode=0o700, parents=True)
    target = root/'alertmanager.yml'
    secretfile = root/'secrets/smtp-password'
    authorization = state/'external-test-authorization.json'
    for p in [target, secretfile, authorization]:
        if p.exists():
            shutil.copyfile(p, backup/p.name)
    previous_secret = secretfile.read_bytes() if secretfile.exists() else None
    previous_config = target.read_bytes()
    previous_auth = authorization.read_bytes() if authorization.exists() else None
    candidate = backup/'candidate.yml'
    candidate.write_text(yaml.safe_dump(config, sort_keys=False))
    account = pwd.getpwnam('webops-alertmanager')
    try:
        step('write_restricted_secret')
        secretfile.write_text(secret)
        os.chown(secretfile, account.pw_uid, account.pw_gid)
        secretfile.chmod(0o400)
        amtool = '/opt/webops-monitoring/alertmanager-0.34.2.linux-amd64/amtool'
        step('amtool_check_config')
        result = subprocess.run([amtool, 'check-config', str(candidate)], capture_output=True, timeout=30)
        print('STAGE09_EMAIL_AMTOOL_EXIT='+str(result.returncode), flush=True)
        assert result.returncode == 0, 'Private candidate failed validation'
        shutil.copyfile(candidate, target)
        os.chown(target, 0, account.pw_gid)
        target.chmod(0o640)
        step('restart_and_readiness')
        subprocess.run(['systemctl', 'restart', 'webops-alertmanager'], check=True, capture_output=True, timeout=30)
        for attempt in range(20):
            try:
                with urllib.request.urlopen('http://127.0.0.1:9093/-/ready', timeout=3) as response:
                    assert response.status == 200
                break
            except Exception:
                if attempt == 19:
                    raise RuntimeError('Readiness failed') from None
                time.sleep(2)
        step('record_authorization')
        authorization.write_text(json.dumps({'channel': 'email', 'recipient': recipient,
            'authorized_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'scope': 'stage09 controlled tunnel failure and recovery'}))
        authorization.chmod(0o600)
    except (Exception, KeyboardInterrupt):
        target.write_bytes(previous_config)
        for path, content in [(secretfile, previous_secret), (authorization, previous_auth)]:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(['systemctl', 'restart', 'webops-alertmanager'], capture_output=True, timeout=30)
        raise
    print('STAGE09_EMAIL_CONFIGURED_LOCALLY; external reception still requires drill and recipient evidence')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('STAGE09_EMAIL_SETUP_INCOMPLETE step='+STEP+' type='+type(error).__name__+'; no secret values printed')
        raise SystemExit(1)
