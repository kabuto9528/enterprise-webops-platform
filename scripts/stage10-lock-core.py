"""Lock the chosen LTS RPM to its checksum in official repository metadata."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

source = Path(sys.argv[1])
output = Path(sys.argv[2])
ns = {"m": "http://linux.duke.edu/metadata/common"}
root = ET.fromstring(gzip.decompress((source / "primary.xml.gz").read_bytes()))
packages = [p for p in root.findall("m:package", ns)
            if p.findtext("m:name", namespaces=ns) == "jenkins"
            and p.find("m:version", ns).get("ver") == "2.580.2"]
assert len(packages) == 1
p = packages[0]
checksum = p.find("m:checksum", ns)
assert checksum.get("type") == "sha256" and len(checksum.text) == 64
href = p.find("m:location", ns).get("href")
assert href.startswith("jenkins-2.580.2-") and href.endswith(".noarch.rpm") and "/" not in href
lock = {"version": "2.580.2", "release": p.find("m:version", ns).get("rel"),
        "filename": href, "url": "https://pkg.jenkins.io/rpm-stable/" + href,
        "sha256": checksum.text, "metadata": "https://pkg.jenkins.io/rpm-stable/repodata/repomd.xml",
        "repositoryKeyUrl": "https://pkg.jenkins.io/rpm-stable/repodata/repomd.xml.key",
        "repositoryKeySha256": hashlib.sha256((source / "jenkins-repository.key").read_bytes()).hexdigest()}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
print(json.dumps(lock))
