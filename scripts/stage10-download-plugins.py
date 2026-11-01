"""Download the reviewed, pinned mandatory plugin closure; verify every artifact."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
target = Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)
mirror = sys.argv[3].rstrip("/") if len(sys.argv) > 3 else None
if mirror not in (None, "https://ftp.yz.yamagata-u.ac.jp/pub/misc/jenkins"):
    raise ValueError("Mirror was not reviewed against the official redirect")


def fetch(item):
    name = item["name"]
    path = target / (name + ".jpi")
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]:
        return name + " VERIFIED_CACHE"
    url = (f"{mirror}/plugins/{name}/{item['version']}/{name}.hpi" if mirror else item["url"])
    result = subprocess.run(["curl.exe", "-fsSL", "--connect-timeout", "10", "--max-time", "300",
                             "--retry", "1", url, "--output", str(path)], capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{name} transport exit{result.returncode}; no artifact accepted")
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError(name + " SHA256 mismatch")
    return name + " DOWNLOADED_SHA256_OK"


with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(fetch, item) for item in lock["plugins"]]
    for future in concurrent.futures.as_completed(futures):
        print(future.result(), flush=True)
print("ALL_LOCKED_PLUGINS_VERIFIED", flush=True)
