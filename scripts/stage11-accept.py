#!/usr/bin/python3
"""Read-only, root-side Stage 11 evidence collection. Never deploys or restarts.

Read Jenkins' direct build/result element, not nested checkout results. Export
only explicit non-secret fields; credentials, inventories and raw logs stay on
the node. Collection success alone does not constitute acceptance.
"""

import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path('/var/lib/webops-ci')
JOB = Path('/var/lib/jenkins/jobs/webops-ci')
SHA = re.compile(r'[0-9a-f]{40}')
RESULTS = {'SUCCESS', 'FAILURE', 'ABORTED', 'UNSTABLE', 'NOT_BUILT'}


def sha(value):
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def metadata(path):
    try:
        info = path.lstat()
        return {'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid,
                'gid': info.st_gid, 'symlink': path.is_symlink()}
    except OSError:
        return {'missing': True}


def digest(path):
    try:
        if path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def scan(text):
    return {
        'private_key_hits': len(re.findall(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', text)),
        'password_assignment_hits': len(re.findall(
            r'(?im)^\s*(?:export\s+)?(?:password|passwd|smtp_auth_password|api_token)\s*=\s*\S+', text)),
        'environment_dump_hits': len(re.findall(r'(?m)^\+ (?:env|printenv|set)\s*$', text)),
    }


def release_summary(document):
    runtime = document.get('runtime') or {}
    external = document.get('external') or {}
    return {
        'build': str(document.get('build', '')) if re.fullmatch(r'[0-9]{1,7}', str(document.get('build', ''))) else None,
        'sha': sha(document.get('sha')),
        'status': document.get('status') if document.get('status') in {'succeeded', 'failed', 'rejected'} else None,
        'ansible_rc': document.get('ansible_rc') if isinstance(document.get('ansible_rc'), int) else None,
        'current_sha': sha(document.get('current_sha')),
        'previous_sha': sha(document.get('previous_sha')),
        'recovery': document.get('recovery') if document.get('recovery') in {'succeeded', 'failed', 'unknown', 'not_needed', 'not_switched', 'no_verified_previous'} else None,
        'archive_sha256': document.get('archive_sha256') if re.fullmatch(r'[0-9a-f]{64}', str(document.get('archive_sha256', ''))) else None,
        'wheel_manifest_sha256': document.get('wheel_manifest_sha256') if re.fullmatch(r'[0-9a-f]{64}', str(document.get('wheel_manifest_sha256', ''))) else None,
        'external_healthz': external.get('healthz') if isinstance(external.get('healthz'), int) else None,
        'external_version_status': external.get('version_status') if isinstance(external.get('version_status'), int) else None,
        'external_version': sha(external.get('version_value')),
        'image_revision': sha(runtime.get('image_revision')),
        'version_env': sha(runtime.get('version_env')),
        'docker_healthy': runtime.get('docker_health') == 'healthy',
        'image_tag_matches': runtime.get('image_tag') == 'webops-app:' + str(document.get('current_sha')),
        'tag_matches_running_image': bool(runtime.get('image_id')) and runtime.get('image_id') == runtime.get('tag_image_id'),
        'detail_present': bool(document.get('detail')),
    }


def collect_build(directory):
    report = {'number': int(directory.name)}
    try:
        root = ET.parse(directory / 'build.xml').getroot()
        result = root.findtext('result')
        report.update(result=result if result in RESULTS else None,
                      in_progress=result is None,
                      duration_ms=root.findtext('duration'),
                      scm_trigger=any('SCMTriggerCause' in element.attrib.get('class', '')
                                      or element.tag.endswith('SCMTriggerCause')
                                      for element in root.iter()),
                      nested_success_count=sum(element.text == 'SUCCESS'
                                               for element in root.iter('result')))
    except (OSError, ET.ParseError):
        report['xml_unreadable'] = True
    try:
        log = (directory / 'log').read_text(encoding='utf-8', errors='replace')
    except OSError:
        log = ''
    report['console_scan'] = scan(log)
    report['checkout_shas'] = sorted(set(re.findall(
        r'(?:CHECKED_OUT_COMMIT|TARGET_SHA)=([0-9a-f]{40})\b', log)))
    report['pytest_passed'] = [int(item) for item in re.findall(r'\b(\d+) passed\b', log)]
    report['section_failures'] = re.findall(r'SECTION_FAILED=([A-Za-z0-9_]+)', log)
    report['pipeline_exit_codes'] = [int(item) for item in re.findall(r'PIPELINE_EXIT=([0-9]+)', log)]
    report['console_finished'] = re.findall(r'Finished: (SUCCESS|FAILURE|ABORTED|UNSTABLE|NOT_BUILT)', log)
    report['in_progress'] = report.get('result') is None or not report['console_finished']
    artifact = directory / 'archive' / 'release-evidence.txt'
    report['evidence_archive'] = artifact.is_file() and not artifact.is_symlink()
    report['source_archives'] = [
        {'sha': sha(item.stem.removeprefix('webops-')), 'bytes': item.stat().st_size,
         'sha256': digest(item)}
        for item in (directory / 'archive' / 'build').glob('webops-*.tar')
        if not item.is_symlink() and item.is_file()
    ]
    result_artifact = directory / 'archive' / 'build' / 'release-result.json'
    try:
        report['archived_release'] = release_summary(json.loads(result_artifact.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        report['archived_release'] = None
    try:
        report['release'] = release_summary(json.loads(
            (BASE / 'results' / (directory.name + '.json')).read_text(encoding='utf-8')))
    except (OSError, ValueError):
        report['release'] = None
    return report


def collect():
    report = {'schema': 'stage11-acceptance/v1',
              'collected_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    report['bare_head'] = sha(command(['git', '--git-dir=/srv/git/webops.git', 'rev-parse', 'refs/heads/main'])[1])
    report['executor_hashes'] = {
        'installed': digest(Path('/usr/local/sbin/webops-ci-release')),
        'uploaded': digest(Path('/home/ops/webops-stage11/candidate/scripts/webops-ci-release.py')),
    }
    report['permissions'] = {str(path): metadata(path) for path in (
        BASE / 'requests', BASE / 'results', BASE / 'work', BASE / 'pipeline',
        Path('/etc/webops/ci/inventory.yml'), Path('/usr/local/sbin/webops-ci-release'))}
    report['jenkins_boundary'] = {}
    for label, args in {
        'sudo_allowed': ['sudo', '-u', 'jenkins', 'sudo', '-n', 'true'],
        'bare_writable': ['sudo', '-u', 'jenkins', 'test', '-w', '/srv/git/webops.git'],
        'root_clone_readable': ['sudo', '-u', 'jenkins', 'test', '-r', str(BASE / 'work')],
        'inventory_readable': ['sudo', '-u', 'jenkins', 'test', '-r', '/etc/webops/ci/inventory.yml'],
    }.items():
        report['jenkins_boundary'][label] = command(args)[0] == 0
    try:
        config = ET.parse(JOB / 'config.xml').getroot()
        triggers = config.find('.//triggers')
        scm_url = config.findtext('definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/url')
        report['job'] = {
            'scm_pipeline': config.find('definition').attrib.get('class', '').endswith('CpsScmFlowDefinition'),
            'jenkinsfile': config.findtext('definition/scriptPath') == 'Jenkinsfile',
            'trusted_bare': scm_url in {'/srv/git/webops.git', 'http://127.0.0.1:19418/webops.git'},
            'loopback_smart_http': scm_url == 'http://127.0.0.1:19418/webops.git',
            'main_branch': config.findtext('definition/scm/branches/hudson.plugins.git.BranchSpec/name') == '*/main',
            'disable_concurrent': any(element.tag.endswith('DisableConcurrentBuildsJobProperty') for element in config.iter()),
            'poll_specs': [element.text for element in triggers.iter('spec')] if triggers is not None else [],
            'trigger_types': [element.tag for element in triggers] if triggers is not None else [],
            'retention_days': config.findtext('.//daysToKeep'),
            'retention_builds': config.findtext('.//numToKeep'),
        }
    except (OSError, ET.ParseError, AttributeError):
        report['job'] = {'unreadable': True}
    directories = sorted((item for item in (JOB / 'builds').glob('*')
                          if item.is_dir() and re.fullmatch(r'[0-9]+', item.name)),
                         key=lambda item: int(item.name))
    report['builds'] = [collect_build(item) for item in directories[-10:]]
    report['services'] = {unit: command(['systemctl', 'show', unit, '-p', 'ActiveState',
                                       '-p', 'Result', '-p', 'ExecMainStatus'])[1].splitlines()
                          for unit in ('jenkins.service', 'webops-ci-release.service', 'webops-ci-release.path', 'webops-git-readonly.service')}
    return report


def main():
    import grp  # Linux-only ownership handling; parsers are portable.

    if os.geteuid() != 0:
        print('Run locally with sudo; this collector does not deploy or restart services.')
        return 2
    report = collect()
    output_dir = BASE / 'acceptance'
    output_dir.mkdir(mode=0o750, exist_ok=True)
    if output_dir.is_symlink():
        raise RuntimeError('Refusing a symlink evidence directory')
    os.chown(output_dir, 0, grp.getgrnam('ops').gr_gid)
    os.chmod(output_dir, 0o750)
    timestamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output = output_dir / ('stage11-acceptance-' + timestamp + '.json')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        os.fchown(handle.fileno(), 0, grp.getgrnam('ops').gr_gid)
        os.fchmod(handle.fileno(), 0o640)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print('STAGE11_EVIDENCE=' + str(output))
    print('BUILDS=' + json.dumps([{'number': item['number'], 'result': item.get('result'),
                                 'scm_trigger': item.get('scm_trigger')} for item in report['builds']]))
    print('STAGE11_ACCEPTANCE=REVIEW_REQUIRED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
