#!/usr/bin/python3
"""Install only hash-locked, previously accepted runtime wheels. Run on ECS.
No package installation, network configuration or application change occurs.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


def main():
    if os.geteuid() != 0:
        raise SystemExit('Requires existing authorized ECS sudo.')
    manifest_file = Path('/home/ecs-user/webops-stage11/runtime-wheels.lock.json')
    raw = manifest_file.read_bytes()
    manifest = json.loads(raw)
    if manifest.get('schema') != 'webops-runtime-wheels/v1':
        raise ValueError('Invalid manifest')
    fingerprint = hashlib.sha256(raw).hexdigest()
    base = Path('/opt/webops/dependencies/runtime-wheels')
    base.mkdir(parents=True, exist_ok=True, mode=0o755)
    destination = base / fingerprint
    source = Path('/home/ecs-user/stage03-linux-wheels')
    entries = manifest.get('files') or []
    if not 1 <= len(entries) <= 100:
        raise ValueError('Invalid entry count')
    payloads = {}
    for item in entries:
        name = item['filename']
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', name) or '..' in name or name in payloads:
            raise ValueError('Unsafe or duplicate wheel name')
        path = source / name
        if path.is_symlink():
            raise ValueError('Refuse a symlink wheel')
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item['sha256'] or len(payload) != item['bytes']:
            raise ValueError('Existing target wheel does not match the reviewed digest')
        payloads[name] = payload
    if destination.exists():
        if destination.is_symlink():
            raise ValueError('Refuse a symlink cache')
        for name, payload in payloads.items():
            if (destination / name).read_bytes() != payload:
                raise ValueError('Existing immutable cache differs')
    else:
        staging = base / ('.staging-' + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        for name, payload in payloads.items():
            target = staging / name
            target.write_bytes(payload)
            os.chmod(target, 0o444)
        os.chmod(staging, 0o555)
        os.rename(staging, destination)
    print('RUNTIME_WHEEL_MANIFEST_SHA256=' + fingerprint)
    print('TARGET_WHEEL_DIGESTS_MATCH=' + str(len(payloads)))
    print('CACHE_DIRECTORY_MODE=' + oct(destination.stat().st_mode & 0o777))


if __name__ == '__main__':
    main()
