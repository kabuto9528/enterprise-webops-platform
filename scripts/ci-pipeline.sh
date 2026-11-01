#!/bin/bash
# Runs as jenkins. No target key, sudo, Docker or free-form deployment command.
set -euo pipefail
umask 0022
mkdir -p build
exec > >(tee -a "/var/lib/webops-ci/pipeline/${BUILD_NUMBER}.log") 2>&1
finish() {
  rc=$?
  trap - EXIT
  {
    printf 'job=%s\nbuild=%s\ntarget_sha=%s\npipeline_exit=%s\n' "$JOB_NAME" "$BUILD_NUMBER" "$TARGET_SHA" "$rc"
    if [ -f build/package.sha256 ]; then printf 'package_sha256='; cat build/package.sha256; fi
    if [ -f build/release-result.json ]; then cat build/release-result.json; fi
  } > release-evidence.txt
  echo "PIPELINE_EXIT=$rc"
  exit "$rc"
}
trap finish EXIT

echo '[1/5] verify SCM checkout'
printf '%s' "$TARGET_SHA" | grep -Eq '^[0-9a-f]{40}$'
test "$(git rev-parse HEAD)" = "$TARGET_SHA"
echo "CHECKED_OUT_COMMIT=$TARGET_SHA"

echo '[2/5] dependency and unit tests'
PYTHONDONTWRITEBYTECODE=1 /opt/webops/venvs/jenkins-test/bin/python -B -m pytest -q -p no:cacheprovider tests/test_app.py

echo '[3/5] export exact source'
git archive --format=tar "$TARGET_SHA" -o "build/webops-$TARGET_SHA.tar"
test "$(git get-tar-commit-id < "build/webops-$TARGET_SHA.tar")" = "$TARGET_SHA"
sha256sum "build/webops-$TARGET_SHA.tar" | cut -d' ' -f1 > build/package.sha256
echo "PACKAGE_COMMIT=$TARGET_SHA"
echo "PACKAGE_SHA256=$(cat build/package.sha256)"

echo '[4/5] request fixed root executor'
request="/var/lib/webops-ci/requests/${BUILD_NUMBER}.json"
result="/var/lib/webops-ci/results/${BUILD_NUMBER}.json"
printf '{"build":"%s","sha":"%s"}\n' "$BUILD_NUMBER" "$TARGET_SHA" > "$request.tmp"
mv "$request.tmp" "$request"
for ((n=0; n<1140; n++)); do
  if [ -s "$result" ]; then break; fi
  sleep 5
done
test -s "$result"
cp "$result" build/release-result.json

echo '[5/5] verify deployment result, archive and runtime'
python3 - "$TARGET_SHA" <<'PY'
import json, pathlib, sys
r = json.loads(pathlib.Path('build/release-result.json').read_text())
expected = sys.argv[1]
external = r.get('external') or {}
runtime = r.get('runtime') or {}
print('RESULT_STATUS=' + str(r.get('status')))
print('RESULT_SHA=' + str(r.get('sha')))
print('RESULT_PREVIOUS=' + str(r.get('previous_sha')))
print('RESULT_RECOVERY=' + str(r.get('recovery')))
print('RECOVERED_VERSION=' + str(r.get('current_sha')))
print('EXTERNAL_HEALTHZ=' + str(external.get('healthz')))
print('EXTERNAL_VERSION=' + str(external.get('version_value')))
if r.get('status') != 'succeeded':
    print('DEPLOY_FAILED=1')
    sys.exit(1)
digest = pathlib.Path('build/package.sha256').read_text().strip()
checks = [r.get('sha') == expected, r.get('current_sha') == expected,
          r.get('archive_sha256') == digest, external.get('healthz') == 200,
          external.get('version_status') == 200, external.get('version_value') == expected,
          runtime.get('image_revision') == expected, runtime.get('version_env') == expected,
          runtime.get('image_tag') == 'webops-app:' + expected,
          runtime.get('image_id') == runtime.get('tag_image_id'),
          runtime.get('docker_health') == 'healthy']
if not all(checks):
    print('SHA_CHAIN_VERIFY_FAILED=1')
    sys.exit(1)
print('VERIFY_OK=' + expected)
PY
