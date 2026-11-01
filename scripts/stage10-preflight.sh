#!/usr/bin/env bash
# Read-only readiness evidence. May run as ops or, for LVM details, under sudo.
# Never prints inventories, credentials, environment variables, or journal logs.
set -u
export LC_ALL=C
probe() {
  printf '\nCOMMAND=%s\n' "$*"
  "$@"
  rc=$?
  printf 'COMMAND_EXIT=%s\n' "$rc"
}
printf 'STAGE10_READ_ONLY_PREFLIGHT\n'
probe date -Is
probe whoami
probe hostname
probe cat /etc/os-release
probe python3 --version
probe git --version
probe nproc
probe free -h
probe df -hT /
probe df -B1 /
probe lsblk -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
probe lsblk -b -o NAME,START,SIZE,TYPE
probe vmstat 1 3
probe java -version
probe systemctl is-active jenkins
probe systemctl show jenkins -p LoadState -p ActiveState -p SubState
probe getent passwd jenkins
probe git --git-dir=/srv/git/webops.git rev-parse HEAD
probe stat -c '%a %U %G %n' /srv/git /srv/git/webops.git /home/ops/.venvs/webops-ansible
probe systemctl is-active nginx mysqld webops-node-exporter
probe getenforce
# Preserve the stage-required ss command; mask non-loopback addresses before output.
printf '\nCOMMAND=ss -lnt (non-loopback addresses redacted)\n'
ss -lnt | python3 -c 'import sys,re
for line in sys.stdin:
    line=re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",lambda m:m[0] if m[0] in ("127.0.0.1","0.0.0.0") else "[private-address]",line)
    line=re.sub(r"\[(?!::\])[^\]]+\]", "[private-address]",line)
    sys.stdout.write(line)'
codes=("${PIPESTATUS[@]}")
printf 'SS_EXIT=%s REDACTION_EXIT=%s\n' "${codes[0]}" "${codes[1]}"
if [ "$(id -u)" -eq 0 ]; then
  probe pvs --units b --nosuffix -o pv_name,pv_size,pv_free,vg_name
  probe vgs --units b --nosuffix -o vg_name,vg_size,vg_free
  probe lvs --units b --nosuffix -o lv_name,vg_name,lv_size,lv_attr
  probe parted -s /dev/sda unit s print free
  printf '\nCOMMAND=sudo -n -u jenkins git --git-dir=/srv/git/webops.git rev-parse HEAD\n'
  sudo -n -u jenkins git --git-dir=/srv/git/webops.git rev-parse HEAD
  printf 'COMMAND_EXIT=%s\n' "$?"
else
  printf '\nROOT_LAYOUT=NOT_COLLECTED_REQUIRES_LOCAL_SUDO\n'
fi
printf '\nSTAGE10_PREFLIGHT_COLLECTION_FINISHED_NOT_ACCEPTANCE\n'
