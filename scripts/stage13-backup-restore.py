#!/usr/bin/python3
"""Local sudo only: consistent project backup and isolated file-level restore.

No original file is removed or overwritten. Secrets and the restored copy remain
under root:root 0700. Only an allowlisted summary is exported to the local admin.
This verifies data/configuration recovery, not a replacement-host boot or DR RTO.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import stat
import subprocess
import tarfile
import xml.etree.ElementTree as ET


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(roots):
    result = {}
    def visit(path):
        info = path.lstat()
        row = dict(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)
        if path.is_symlink():
            row.update(kind='link', target=os.readlink(path))
        elif path.is_dir():
            row['kind'] = 'directory'
        elif path.is_file():
            row.update(kind='file', bytes=info.st_size, sha256=digest(path))
        else:
            raise RuntimeError('Unexpected special file in selected project data')
        result[str(path).lstrip('/')] = row
        if row['kind'] == 'directory':
            for child in sorted(path.iterdir()):
                visit(child)
    for path in roots:
        visit(path)
    return result


def restore(archive, destination):
    # Never extract through a symlink. Links are created after regular files;
    # no archive-provided absolute path, parent traversal or device is accepted.
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if Path(member.name).is_absolute() or '..' in parts:
                raise RuntimeError('Unsafe archive path')
            if not (member.isdir() or member.isfile() or member.issym()):
                raise RuntimeError('Unexpected archive member type')
        for member in sorted(members, key=lambda m: (m.issym(), len(Path(m.name).parts))):
            target = destination / member.name
            for parent in target.parents:
                if parent == destination:
                    break
                if parent.is_symlink():
                    raise RuntimeError('Symlink ancestor rejected')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.issym():
                target.symlink_to(member.linkname)
            else:
                with bundle.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
            os.chown(target, member.uid, member.gid, follow_symlinks=False)
            if not member.issym():
                os.chmod(target, member.mode)


def main():
    node = socket.gethostname()
    if os.geteuid() != 0 or node not in {'ops01', 'monitor01', 'web01-hz'}:
        raise SystemExit('Run locally with sudo on a documented project node.')
    os.umask(0o077)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base = Path('/var/lib/webops-stage13')
    base.mkdir(exist_ok=True, mode=0o700)
    if base.is_symlink() or base.stat().st_uid != 0 or stat.S_IMODE(base.stat().st_mode) != 0o700:
        raise RuntimeError('Backup parent must be root-owned mode0700')
    work = base / stamp
    work.mkdir(mode=0o700)
    log = (work / 'private.log').open('x')
    report = dict(node=node, started_utc=stamp, status='INCOMPLETE',
                  backup_directory=str(work), scope='isolated file/data recovery; no replacement-host boot',
                  checks=[], services_restored=False)
    def run(args, seconds=90):
        p = subprocess.run(args, stdout=log, stderr=log, timeout=seconds)
        log.flush()
        if p.returncode:
            raise RuntimeError('Command failed; inspect root-private log')
    def active(unit):
        return subprocess.run(['systemctl','is-active','--quiet',unit], timeout=10).returncode == 0
    units = {'ops01':['jenkins','webops-ci-release.path'],
             'monitor01':['webops-prometheus','webops-grafana','webops-alertmanager'],
             'web01-hz':[]}[node]
    original = {u:active(u) for u in units}
    if not all(original.values()):
        raise RuntimeError('Expected services are not all active; no backup started')
    if node == 'ops01':
        if active('webops-ci-release.service'):
            raise RuntimeError('CI executor is busy')
        if list(Path('/var/lib/webops-ci/requests').glob('*.json')):
            raise RuntimeError('Pending CI request; defer backup')
        for build in Path('/var/lib/jenkins/jobs/webops-ci/builds').glob('*/build.xml'):
            if ET.parse(build).getroot().findtext('result') is None:
                raise RuntimeError('Jenkins build in progress')
    selected = {
        'ops01':['/etc/webops/ci','/srv/git/webops.git','/var/lib/webops-ci/work/ansible',
                 '/usr/local/sbin/webops-ci-release','/usr/local/libexec/webops-git-readonly.py',
                 '/etc/systemd/system/webops-ci-release.service','/etc/systemd/system/webops-ci-release.path',
                 '/etc/systemd/system/webops-git-readonly.service','/etc/systemd/system/jenkins.service.d'],
        'monitor01':['/etc/webops-monitoring','/var/lib/webops-monitoring',
                     '/var/lib/webops-metrics-tunnel/.ssh','/opt/webops-monitoring/bin'],
        'web01-hz':['/etc/nginx','/var/lib/webops-release']
    }[node]
    if node == 'ops01':
        home = Path('/var/lib/jenkins')
        selected += [str(p) for p in home.iterdir() if p.is_file() or p.name in {'jobs','users','secrets','plugins','nodes'}]
    if node == 'web01-hz':
        current = json.loads(Path('/var/lib/webops-release/current.json').read_text())
        # Save all recorded healthy Compose/source directories, never Docker layers.
        for version in Path('/var/lib/webops-release/versions').glob('*.json'):
            data = json.loads(version.read_text())
            if data.get('known_healthy'):
                compose = Path(data['compose']) if 'compose' in data else None
                if compose and compose.is_file():
                    selected.append(str(compose.parent))
    candidates = sorted({Path(p) for p in selected}, key=lambda p: len(p.parts))
    roots = [p for p in candidates if not any(parent in candidates for parent in p.parents)]
    if not all(p.exists() for p in roots):
        raise RuntimeError('Required backup source is missing')
    # Live TSDB/WAL entries can disappear before the services are stopped.
    # Preflight estimates space only; hashes are collected after quiescence.
    size = 0
    for path in roots:
        entries = [path] if not path.is_dir() else path.rglob('*')
        for entry in entries:
            try:
                info=entry.lstat()
                if stat.S_ISREG(info.st_mode):size += info.st_size
            except FileNotFoundError:
                continue
    if shutil.disk_usage(work).free < size*3 + 1024**3:
        raise RuntimeError('Insufficient backup/restore free space')
    timer = 'webops-stage13-restart-'+stamp.lower()
    timer_armed = False
    try:
        if units:
            run(['systemd-run','--unit='+timer,'--on-active=5m','/usr/bin/systemctl','start',*units])
            run(['systemctl','is-active','--quiet',timer+'.timer'])
            timer_armed = True
            # Stop path first so a new request cannot start a deployment.
            run(['systemctl','stop',*reversed(units)])
        before = inventory(roots)
        archive = work / 'backup.tar.gz'
        with tarfile.open(archive,'w:gz',dereference=False) as bundle:
            for path in roots:
                bundle.add(path, arcname=str(path).lstrip('/'), recursive=True)
        os.chmod(archive,0o600)
        after = inventory(roots)
        if before != after:
            raise RuntimeError('Source changed during backup; inconsistent snapshot')
        (work/'manifest.private.json').write_text(json.dumps(before,sort_keys=True))
        destination = work/'restore'
        destination.mkdir(mode=0o700)
        restore(archive,destination)
        mismatches = []
        restored = inventory([destination / str(p).lstrip('/') for p in roots])
        for name, expected in before.items():
            value = restored[str(destination/name).lstrip('/')]
            if value != expected:
                mismatches.append(name)
        if mismatches:
            raise RuntimeError('Restored content/ownership/modes differ')
        report['checks'].append('all restored files SHA256/size/mode/uid/gid and links match')
        report.update(archive_bytes=archive.stat().st_size, archive_sha256=digest(archive),
                      restored_entries=len(before), regular_files=sum(x['kind']=='file' for x in before.values()))
        if node == 'ops01':
            restored_home=destination/'var/lib/jenkins'
            xml_files=set(restored_home.glob('*.xml'))
            xml_files.update((restored_home/'jobs').rglob('config.xml'))
            xml_files.update((restored_home/'jobs').rglob('build.xml'))
            xml_files.update((restored_home/'users').rglob('config.xml'))
            for path in xml_files:
                ET.parse(path)
            restored_git=str(destination/'srv/git/webops.git')
            run(['git','-c','safe.directory='+restored_git,'--git-dir='+restored_git,'fsck','--full'])
            report['checks'] += ['restored Jenkins XML parsed including jobs/users/configuration', 'restored bare Git fsck passed']
        if node == 'monitor01':
            databases = list((destination/'var/lib/webops-monitoring').rglob('grafana.db'))
            if len(databases) != 1:
                raise RuntimeError('Expected exactly one restored Grafana database')
            with sqlite3.connect(databases[0].as_uri()+'?mode=ro',uri=True) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Restored Grafana DB integrity failed')
                report['restored_grafana_dashboards'] = db.execute('SELECT count(*) FROM dashboard').fetchone()[0]
            for name in ['prometheus','alertmanager']:
                restored_config=destination/'etc/webops-monitoring'/name/(name+'.yml')
                if not restored_config.is_file():
                    raise RuntimeError('Missing restored monitoring configuration')
                checked = work/(name+'-restored-check.yml')
                checked.write_text(restored_config.read_text().replace('/etc/webops-monitoring/',str(destination/'etc/webops-monitoring')+'/'))
                if name == 'prometheus':
                    binaries = list(Path('/opt/webops-monitoring').glob('prometheus-*/promtool'))
                    if len(binaries)!=1:raise RuntimeError('Ambiguous promtool')
                    run([str(binaries[0]),'check','config',str(checked)])
                else:
                    binaries = list(Path('/opt/webops-monitoring').glob('alertmanager-*/amtool'))
                    if len(binaries)!=1:raise RuntimeError('Ambiguous amtool')
                    run([str(binaries[0]),'check-config',str(checked)])
            dashboard=json.loads((destination/'etc/webops-monitoring/grafana/dashboards/webops-overview.json').read_text())
            report['restored_grafana_dashboard_panels']=len(dashboard['panels'])
            assert report['restored_grafana_dashboard_panels']==7
            report['checks'] += ['restored Grafana SQLite integrity ok', 'restored Grafana provisioning dashboard has 7 panels', 'restored Prometheus and Alertmanager configs validated']
        if node == 'web01-hz':
            run(['nginx','-t'])
            report['checks'].append('live nginx -t passed; restored Nginx files match byte-for-byte')
        report['status']='FILE_RESTORE_VERIFIED'
    except Exception as error:
        report['failure_type']=type(error).__name__
        raise
    finally:
        if timer_armed:
            try:
                run(['systemctl','start',*units],180)
                report['services_restored']=all(active(u) for u in units)
                if report['services_restored']:
                    run(['systemctl','stop',timer+'.timer'])
            except Exception:
                report['services_restored']=False
        else:
            report['services_restored']=not units
        report['finished_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        (work/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        user={'ops01':'ops','monitor01':'monitor','web01-hz':'ecs-user'}[node]
        result_dir=Path('/var/lib/webops-stage13-results')
        result_dir.mkdir(mode=0o750,exist_ok=True)
        info=result_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:
            raise RuntimeError('Unsafe summary directory')
        os.chown(result_dir,0,pwd.getpwnam(user).pw_gid)
        os.chmod(result_dir,0o750)
        output=result_dir/'backup-result.json'
        if output.is_symlink():raise RuntimeError('Refuse symlink report destination')
        output.write_text(json.dumps(report,indent=2)+'\n')
        os.chown(output,0,pwd.getpwnam(user).pw_gid)
        os.chmod(output,0o640)
        print(json.dumps(report))
    if report['status']!='FILE_RESTORE_VERIFIED' or not report['services_restored']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
