node {
  timeout(time: 10, unit: 'MINUTES') {
    try {
      deleteDir()
      sh '''#!/bin/sh
set -eu
git clone --no-local --no-hardlinks --no-checkout /srv/git/webops.git source
cd source
git checkout --detach 48c4ee51a408e574a83e08e900680641821060bc
actual=$(git rev-parse HEAD)
test "$actual" = 48c4ee51a408e574a83e08e900680641821060bc
printf 'CHECKED_OUT_SHA=%s\n' "$actual"
git --version
python3 --version
java -version
/opt/webops/venvs/jenkins-ansible/bin/ansible --version | head -n 1
export PYTHONDONTWRITEBYTECODE=1
/opt/webops/venvs/jenkins-test/bin/python -m pytest -q -p no:cacheprovider tests/test_app.py
'''
    } finally {
      deleteDir()
    }
  }
}
