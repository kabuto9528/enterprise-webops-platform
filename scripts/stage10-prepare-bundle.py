"""Create an explicit, secret-free installation bundle; never include .local configuration."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

repo = Path(__file__).resolve().parents[1]
target = repo / ".local/stage10-bundle"
target.mkdir(parents=True, exist_ok=True)
files = ["scripts/stage10-install.sh",
         "jenkins/stage10-init.groovy", "jenkins/stage10-job.groovy",
         "jenkins/core.lock.json", "jenkins/plugins.lock.json",
         "requirements.txt", "requirements-dev.txt", "ansible/requirements-control.txt"]
for relative in files:
    dest = target / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / relative, dest)
core = json.loads((repo / "jenkins/core.lock.json").read_text())
for source, relative, digest in [
    (repo / ".local/stage10-downloads/mirror" / core["filename"], "packages/" + core["filename"], core["sha256"]),
    (repo / ".local/stage10-downloads/jenkins-repository.key", "packages/jenkins-repository.key", core["repositoryKeySha256"]),
]:
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    (target / relative).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target / relative)
    files.append(relative)
plugins = json.loads((repo / "jenkins/plugins.lock.json").read_text())["plugins"]
for plugin in plugins:
    relative = "plugins/" + plugin["name"] + ".jpi"
    source = repo / ".local/stage10-downloads" / relative
    assert hashlib.sha256(source.read_bytes()).hexdigest() == plugin["sha256"], plugin["name"]
    (target / relative).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target / relative)
    files.append(relative)
manifest = "".join(hashlib.sha256((target / relative).read_bytes()).hexdigest() + "  " + relative + "\n"
                   for relative in sorted(files))
(target / "bundle.sha256").write_text(manifest, encoding="ascii")
archive = repo / ".local/stage10-bundle.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    for relative in sorted(files) + ["bundle.sha256"]:
        tar.add(target / relative, arcname=relative, recursive=False)
print(f"BUNDLE_FILES={len(files)} PLUGINS={len(plugins)} BYTES={archive.stat().st_size}")
print("BUNDLE_SHA256=" + hashlib.sha256(archive.read_bytes()).hexdigest())
