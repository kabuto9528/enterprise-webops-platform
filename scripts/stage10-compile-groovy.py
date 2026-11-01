"""Compile the public Groovy sources against the exact installed core and locked plugin jars."""
import hashlib
import os
from pathlib import Path
import subprocess
import zipfile

base = Path('/home/ops/webops-stage10')
out = base / 'compile-check'
out.mkdir(exist_ok=True)
jars = []
archives = [Path('/usr/share/java/jenkins.war')] + sorted((base/'plugins').glob('*.jpi'))
for archive in archives:
    with zipfile.ZipFile(archive) as z:
        for item in z.namelist():
            if item.startswith('WEB-INF/lib/') and item.endswith('.jar'):
                data = z.read(item)
                target = out / (hashlib.sha256(data).hexdigest() + '.jar')
                if not target.exists():
                    target.write_bytes(data)
                jars.append(str(target))
env = dict(os.environ, LC_ALL='C.UTF-8', LANG='C.UTF-8')
subprocess.run(['java', '-Xmx384m', '-cp', ':'.join(jars),
                'org.codehaus.groovy.tools.FileSystemCompiler', '-d', str(out/'classes'),
                str(base/'jenkins/stage10-init.groovy'), str(base/'jenkins/stage10-job.groovy')],
               check=True, env=env)
print('GROOVY_COMPILE_WITH_EXACT_CORE_AND_PLUGINS_OK')
