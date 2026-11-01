"""Bounded HTTPS range retries for a flaky official mirror; accept only the locked SHA256."""
import argparse
import concurrent.futures
import hashlib
from pathlib import Path
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("url")
parser.add_argument("size", type=int)
parser.add_argument("sha256")
parser.add_argument("output", type=Path)
args = parser.parse_args()
assert args.url.startswith("https://ftp.yz.yamagata-u.ac.jp/pub/misc/jenkins/")
assert args.size > 0 and len(args.sha256) == 64
args.output.parent.mkdir(parents=True, exist_ok=True)
if args.output.exists() and hashlib.sha256(args.output.read_bytes()).hexdigest() == args.sha256:
    print("VERIFIED_COMPLETE_CACHE", flush=True)
    raise SystemExit(0)
parts = args.output.parent / (args.output.name + ".parts")
parts.mkdir(exist_ok=True)
chunk = 512 * 1024
ranges = [(start, min(start + chunk, args.size) - 1) for start in range(0, args.size, chunk)]


def fetch(bounds):
    start, end = bounds
    path = parts / str(start)
    if path.exists() and path.stat().st_size == end - start + 1:
        return path
    for attempt in range(3):
        try:
            req = urllib.request.Request(args.url, headers={"Range": f"bytes={start}-{end}",
                                                            "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=20) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == f"bytes {start}-{end}/{args.size}"
                data = response.read(end - start + 2)
                assert len(data) == end - start + 1
            path.write_bytes(data)
            return path
        except Exception:
            if attempt == 2:
                raise RuntimeError(f"RANGE_FAILED={start}-{end}; no artifact accepted") from None
            time.sleep(attempt + 1)


with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    jobs = [pool.submit(fetch, bounds) for bounds in ranges]
    for count, future in enumerate(concurrent.futures.as_completed(jobs), 1):
        future.result()
        if count % 8 == 0 or count == len(jobs):
            print(f"RANGES_READY={count}/{len(jobs)}", flush=True)
data = b"".join((parts / str(start)).read_bytes() for start, end in ranges)
assert len(data) == args.size and hashlib.sha256(data).hexdigest() == args.sha256, "ASSEMBLED_SHA256_MISMATCH"
args.output.write_bytes(data)
print("COMPLETE_ARTIFACT_SHA256_OK", flush=True)
