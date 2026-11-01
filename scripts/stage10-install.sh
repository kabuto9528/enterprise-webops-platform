#!/usr/bin/env bash
# Run locally by the user under sudo. Never prints credentials or raw Jenkins journal.
set -euo pipefail
export LC_ALL=C.UTF-8
umask 027
trap 'printf "STAGE10_INSTALL_FAILED_LINE=%s\n" "$LINENO" >&2' ERR
test "$(id -u)" = 0
test "$(hostname -s)" = ops01
awk '/MemTotal:/ { if ($2 < 2621440) exit 1 }' /proc/meminfo
bundle="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$bundle"
printf 'STAGE10_VERIFY_BUNDLE\n'
sha256sum --check bundle.sha256
python3 - <<'PY'
import hashlib,json,pathlib
base=pathlib.Path('.')
c=json.loads((base/'jenkins/core.lock.json').read_text())
assert hashlib.sha256((base/'packages'/c['filename']).read_bytes()).hexdigest()==c['sha256']
assert hashlib.sha256((base/'packages/jenkins-repository.key').read_bytes()).hexdigest()==c['repositoryKeySha256']
p=json.loads((base/'jenkins/plugins.lock.json').read_text())
assert p['jenkinsVersion']==c['version']=='2.580.2' and len(p['plugins'])==32
for x in p['plugins']:
    assert hashlib.sha256((base/'plugins'/(x['name']+'.jpi')).read_bytes()).hexdigest()==x['sha256'],x['name']
print('ALL_CORE_AND_PLUGIN_SHA256_OK')
PY
printf 'STAGE10_CHECK_INSTALLATION_DISK_SPACE\n'
# Storage changes require a separate reviewed maintenance procedure.
python3 - <<'PY'
import shutil
assert shutil.disk_usage('/').free >= 2 * 1024**3, 'At least 2 GiB free space is required for installation'
PY
printf 'STAGE10_INSTALL_JAVA_AND_FONTCONFIG\n'
dnf -y install java-21-openjdk-headless fontconfig
java -version
printf 'STAGE10_CHECK_OFFICIAL_RPM_SIGNATURE\n'
rpmkeys --import "$bundle/packages/jenkins-repository.key"
rpmkeys --checksig --verbose "$bundle/packages/jenkins-2.580.2-1.noarch.rpm"
systemctl mask jenkins.service
dnf -y --disablerepo='*' --setopt=localpkg_gpgcheck=1 install "$bundle/packages/jenkins-2.580.2-1.noarch.rpm"
test "$(rpm -q --qf '%{VERSION}' jenkins)" = 2.580.2
getent group webops-code >/dev/null
usermod -a -G webops-code -s /usr/sbin/nologin jenkins
case " $(id -Gn jenkins) " in *' wheel '*|*' docker '*) printf 'UNEXPECTED_PRIVILEGED_GROUP\n'; exit 1;; esac
install -d -o jenkins -g jenkins -m 0750 /var/lib/jenkins /var/cache/jenkins /var/log/jenkins
install -d -o jenkins -g jenkins -m 0750 /var/lib/jenkins/plugins
install -d -o root -g root -m 0755 /var/lib/webops-stage10
install -d -o root -g root -m 0700 /var/lib/webops-stage10/backups
install -d -o root -g jenkins -m 0750 /var/lib/webops-stage10/bootstrap-secrets
install -o root -g root -m 0644 "$bundle/jenkins/stage10-job.groovy" /var/lib/webops-stage10/stage10-job.groovy
install -o root -g root -m 0644 "$bundle/jenkins/plugins.lock.json" /var/lib/webops-stage10/plugins.lock.json
install -o root -g root -m 0644 "$bundle/jenkins/core.lock.json" /var/lib/webops-stage10/core.lock.json
touch /var/lib/webops-stage10/jenkins-status.json
chown jenkins:jenkins /var/lib/webops-stage10/jenkins-status.json
chmod 0644 /var/lib/webops-stage10/jenkins-status.json
for file in "$bundle"/plugins/*.jpi; do install -o jenkins -g jenkins -m 0644 "$file" /var/lib/jenkins/plugins/; done
printf 'STAGE10_CREATE_ISOLATED_TOOL_ENVIRONMENTS\n'
install -d -o root -g root -m 0755 /opt/webops/venvs
for envname in jenkins-ansible jenkins-test; do
  python3 -m venv "/opt/webops/venvs/$envname"
done
/opt/webops/venvs/jenkins-ansible/bin/python -m pip --isolated --disable-pip-version-check install -q --no-cache-dir --index-url https://pypi.org/simple -r "$bundle/ansible/requirements-control.txt"
/opt/webops/venvs/jenkins-test/bin/python -m pip --isolated --disable-pip-version-check install -q --no-cache-dir --index-url https://pypi.org/simple -r "$bundle/requirements-dev.txt"
/opt/webops/venvs/jenkins-ansible/bin/python -m pip check
/opt/webops/venvs/jenkins-test/bin/python -m pip check
chmod -R u=rwX,go=rX /opt/webops/venvs/jenkins-ansible /opt/webops/venvs/jenkins-test
# runuser inherits cwd; ops home is deliberately0700 and must stay private.
cd /var/lib/jenkins
runuser -u jenkins -- git config --global --replace-all safe.directory /srv/git/webops.git
runuser -u jenkins -- git --git-dir=/srv/git/webops.git rev-parse HEAD
runuser -u jenkins -- /opt/webops/venvs/jenkins-ansible/bin/ansible --version | head -n 1
if [ ! -f /var/lib/webops-stage10/credential-import-complete ]; then
  if [ ! -f /var/lib/webops-stage10/bootstrap-secrets/deploy-key ]; then
    ssh-keygen -q -t ed25519 -N '' -C webops-stage10-deploy -f /var/lib/webops-stage10/bootstrap-secrets/deploy-key
  fi
  chown root:jenkins /var/lib/webops-stage10/bootstrap-secrets/deploy-key
  chmod 0440 /var/lib/webops-stage10/bootstrap-secrets/deploy-key
  install -o root -g root -m 0644 /var/lib/webops-stage10/bootstrap-secrets/deploy-key.pub /var/lib/webops-stage10/deploy-key.pub
fi
install -d -o root -g root -m 0755 /var/lib/jenkins/init.groovy.d /etc/jenkins /etc/systemd/system/jenkins.service.d
install -o root -g root -m 0644 "$bundle/jenkins/stage10-init.groovy" /var/lib/jenkins/init.groovy.d/20-stage10.groovy
cat > /etc/jenkins/logging.properties <<'EOF'
handlers=java.util.logging.ConsoleHandler
.level=INFO
java.util.logging.ConsoleHandler.level=INFO
java.util.logging.ConsoleHandler.formatter=java.util.logging.SimpleFormatter
jenkins.install.SetupWizard.level=OFF
EOF
cat > /etc/systemd/system/jenkins.service.d/10-stage10.conf <<'EOF'
[Service]
Environment="LANG=C.UTF-8"
Environment="LC_ALL=C.UTF-8"
Environment="JAVA_OPTS=-Djava.awt.headless=true -Xms256m -Xmx768m -Djava.util.logging.config.file=/etc/jenkins/logging.properties"
Environment="JENKINS_LISTEN_ADDRESS=127.0.0.1"
Environment="JENKINS_PORT=8080"
Environment="JENKINS_OPTS=--httpListenAddress=127.0.0.1"
MemoryAccounting=true
MemoryHigh=1280M
MemoryMax=1408M
CPUQuota=200%
TasksMax=256
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
UMask=0027
EOF
chmod 0644 /etc/jenkins/logging.properties /etc/systemd/system/jenkins.service.d/10-stage10.conf
systemctl unmask jenkins.service
systemctl daemon-reload
systemctl enable --now jenkins.service
printf 'STAGE10_WAIT_FOR_LOCKED_INITIAL_SETUP\n'
for attempt in $(seq 1 90); do
  if [ -s /var/lib/webops-stage10/jenkins-status.json ] && curl -fsS --max-time 3 http://127.0.0.1:8080/login >/dev/null; then break; fi
  sleep 2
done
python3 - <<'PY'
import json,pathlib,subprocess
s=json.loads(pathlib.Path('/var/lib/webops-stage10/jenkins-status.json').read_text())
p=json.loads(pathlib.Path('/var/lib/webops-stage10/plugins.lock.json').read_text())
assert s['executors']==1 and s['agentPort']==-1 and not s['anonymousRead']
assert s['credentialsPresent'] and not s['credentialTargetAuthorized']
assert set(s['plugins'])=={x['name'] for x in p['plugins']}
for x in p['plugins']:
    assert s['plugins'][x['name']]=={'version':x['version'],'active':True},x['name']
secret=pathlib.Path('/var/lib/jenkins/secrets/initialAdminPassword')
if secret.exists():
    journal=subprocess.check_output(['journalctl','-u','jenkins','-b','--no-pager'],text=True)
    assert secret.read_text().strip() not in journal,'INITIAL_PASSWORD_LOGGING_DETECTED'
print('JENKINS_ACTIVE_MINIMAL_PLUGINS_AND_CREDENTIAL_STORE_OK')
PY
touch /var/lib/webops-stage10/credential-import-complete
chmod 0600 /var/lib/webops-stage10/credential-import-complete
# Exact temporary bootstrap file only; encrypted Jenkins credential and public key remain.
rm -f -- /var/lib/webops-stage10/bootstrap-secrets/deploy-key
systemctl is-active jenkins
systemctl show jenkins -p User -p Group -p MainPID -p MemoryCurrent -p MemoryPeak -p MemoryMax -p CPUQuotaPerSecUSec -p ProtectHome
ss -lnt '( sport = :8080 )'
free -h
df -h /
printf 'STAGE10_INSTALL_READY_LOCAL_ADMIN_SETUP_AND_REAL_JOB_STILL_REQUIRED\n'
