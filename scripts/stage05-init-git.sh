#!/bin/bash
# Run once, interactively with sudo on ops01 after reviewing this file.
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname -s)" = ops01
id ops >/dev/null
command -v git >/dev/null
test ! -e /srv/git/webops.git || { echo 'Repository exists; stop for inspection.' >&2; exit 1; }
if test -e /srv/git; then
  test -d /srv/git && test ! -L /srv/git
  test -z "$(find /srv/git -mindepth 1 -maxdepth 1 -print -quit)" || {
    echo '/srv/git is not empty; stop without changing ownership.' >&2; exit 1;
  }
fi
getent group webops-code >/dev/null || groupadd --system webops-code
usermod -a -G webops-code ops
install -d -o root -g webops-code -m 2750 /srv/git
install -d -o ops -g webops-code -m 2750 /srv/git/webops.git
runuser -u ops -- git init --bare --initial-branch=main --shared=0640 /srv/git/webops.git
runuser -u ops -- git --git-dir=/srv/git/webops.git config receive.denyNonFastForwards true
runuser -u ops -- git --git-dir=/srv/git/webops.git config receive.denyDeletes true
stat -c '%a %U %G %n' /srv/git /srv/git/webops.git
echo STAGE05_GIT_INIT_OK
