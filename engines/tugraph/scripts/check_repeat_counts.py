#!/usr/bin/env python3
"""Does TuGraph keep graph data above LMDB, so a repeated read never reaches the store? Run one query on the
running server twice, restart the server (start-server.sh restores the backup), run it once more, and compare the
three sets of counts, per LMDB table and call. Equal counts: no cache hides KV operations. Exit 1 when they differ.

    SF=0.3 check_repeat_counts.py --query ../queries/microbench/tugraph/hop1p7_knows.cypher --param personId=1050
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REST = f"http://127.0.0.1:{os.environ.get('TUGRAPH_PORT', '7070')}"
STATS = f"http://127.0.0.1:{os.environ.get('TUGRAPH_KVSTATS_PORT', '7072')}"


def call(url, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=600) as r:
        return json.load(r)


def one_run(text, params):
    """(rows, counts per table and call) of one execution; the login comes before the reset"""
    token = call(REST + "/login", {"user": os.environ.get("TUGRAPH_USER", "admin"),
                                   "password": os.environ.get("TUGRAPH_PASSWORD", "73@TuGraph")})["jwt"]
    time.sleep(0.2)                                   # let the login's own reads land before the reset
    call(STATS + "/kvstats/reset", method="PUT")
    out = call(REST + "/cypher", {"graph": "default", "script": text, "parameters": params}, token)
    if "error_message" in out:
        sys.exit(f"check_repeat_counts: {out['error_message']}")
    stats = call(STATS + "/kvstats")
    counts = {f"{table} {name}": (c["count"], c["miss_count"], c["key_bytes"], c["value_bytes"])
              for table, calls in sorted(stats["by_table"].items()) for name, c in sorted(calls.items())}
    return out["size"], counts, {k: stats[k] for k in ("get_count", "seek_count", "next_count", "prev_count")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", required=True, help="a Cypher file")
    ap.add_argument("--param", action="append", default=[], help="name=integer, as often as needed")
    a = ap.parse_args()
    with open(a.query, encoding="utf-8") as f:
        text = "\n".join(ln for ln in f.read().splitlines() if not ln.lstrip().startswith("//"))
    params = {"$" + k: int(v) for k, v in (p.split("=", 1) for p in a.param)}
    runs = [one_run(text, params), one_run(text, params)]
    subprocess.run(["bash", os.path.join(HERE, "..", "start-server.sh")], check=True, stdout=subprocess.DEVNULL)
    runs.append(one_run(text, params))
    names = ("run 1", "run 2", "a fresh server")
    differ = 0
    for key in sorted(set().union(*(r[1] for r in runs))):
        vals = [r[1].get(key) for r in runs]
        if len(set(vals)) > 1:
            differ += 1
            print(f"  differs: {key}: " + ", ".join(f"{n} {v}" for n, v in zip(names, vals)), file=sys.stderr)
    rows, _, total = runs[0]
    name = os.path.splitext(os.path.basename(a.query))[0]
    verdict = "counts equal on" if not differ and len({r[0] for r in runs}) == 1 else f"{differ} counts DIFFER between"
    print(f"tugraph {name} {' '.join(a.param)}: {verdict} run 1, run 2 and a fresh server "
          f"({rows} rows; GET {total['get_count']}, SEEK {total['seek_count']}, "
          f"NEXT {total['next_count'] + total['prev_count']}; {len(runs[0][1])} table-and-call counts compared)")
    for key, v in runs[0][1].items():
        print(f"    {key}: count {v[0]}, miss {v[1]}, key bytes {v[2]}, value bytes {v[3]}")
    sys.exit(1 if differ else 0)


if __name__ == "__main__":
    main()
