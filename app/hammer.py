"""python -m app.hammer: steady traffic against the service while something changes.

Runs inside the cluster as a one-off pod, so requests go through the Service and get spread
across pods the same way real traffic would. Prints how many requests failed and which
versions and pods answered, then exits non-zero if anything failed.

    python -m app.hammer --url http://ledger-api/version --seconds 60
"""
import argparse
import sys
import time
from collections import Counter

import httpx


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--rate", type=float, default=20, help="requests per second")
    a = ap.parse_args(argv)

    ok = failed = 0
    versions, pods, errors = Counter(), Counter(), Counter()
    end = time.monotonic() + a.seconds
    gap = 1 / a.rate
    last_report = time.monotonic()
    while time.monotonic() < end:
        t0 = time.monotonic()
        try:
            # a fresh connection each time, so every request is load-balanced again
            r = httpx.get(a.url, timeout=2)
            if r.status_code == 200:
                ok += 1
                body = r.json()
                versions[body.get("version")] += 1
                pods[body.get("pod")] += 1
            else:
                failed += 1
                errors[f"HTTP {r.status_code}"] += 1
        except httpx.HTTPError as e:
            failed += 1
            errors[type(e).__name__] += 1
        if time.monotonic() - last_report >= 10:
            print(f"  {ok + failed:>5} requests, {failed} failed, versions {dict(versions)}", flush=True)
            last_report = time.monotonic()
        time.sleep(max(0, gap - (time.monotonic() - t0)))

    print(f"hammer: {ok + failed} requests over {a.seconds:.0f} s, {ok} ok, {failed} failed")
    print(f"  versions seen: {dict(versions)}")
    print(f"  pods that answered: {len(pods)}")
    if errors:
        print(f"  errors: {dict(errors)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
