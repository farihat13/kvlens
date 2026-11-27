"""LDBC's fetch step (python/kvevaluator/datasets/ldbc/fetch.py, run by its file) pins, fetches and checks LDBC archives, and refuses a wrong one: a changed archive on the host, a changed or
extra file in place, a re-pin, an SF not pinned. Runs on a local HTTP server serving small fake archives (a missing one
is a 404, pinned as not hosted; a dropped download is retried), a temp manifest and temp data dirs; nothing is downloaded.

    python tests/unit/check_fetch_ldbc.py
"""
import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from kvevaluator.paths import ROOT

VAR = "KVEVAL_DATA_DIR"
SF = "9"


def make_archive(host, rel, top, files):
    """host/rel: a .tar.zst of one dir `top` holding files {relative path: text}"""
    with tempfile.TemporaryDirectory() as t:
        for p, text in files.items():
            (Path(t) / top / p).parent.mkdir(parents=True, exist_ok=True)
            (Path(t) / top / p).write_text(text)
        (host / rel).parent.mkdir(parents=True, exist_ok=True)
        (host / rel).unlink(missing_ok=True)
        subprocess.run(["tar", "--use-compress-program=zstd -q", "-cf", str(host / rel), "-C", t, top], check=True)


class Flaky(http.server.SimpleHTTPRequestHandler):
    """serves the fake host; a path in `drops` sends half its bytes and hangs up, that many times"""
    drops = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.drops.get(self.path, 0) > 0:
            self.drops[self.path] -= 1
            data = (Path(self.directory) / self.path.lstrip("/")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[: len(data) // 2])
            self.close_connection = True
            return
        super().do_GET()


def main():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail[-600:]}" if detail and not ok else ""))
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        host, manifest = d / "host", d / "archives.json"
        csv_rel = f"snb-interactive-v1/social_network-sf{SF}-CsvBasic-LongDateFormatter.tar.zst"
        make_archive(host, csv_rel, f"social_network-sf{SF}-CsvBasic-LongDateFormatter",
                     {"static/place_0_0.csv": "id|name\n1|x\n", "dynamic/person_0_0.csv": "id\n7\n"})
        make_archive(host, f"snb-interactive-v1/substitution_parameters-sf{SF}.tar.zst", f"substitution_parameters-sf{SF}",
                     {"interactive_1_param.txt": "personId|firstName\n7|A\n"})
        # no updates archive: the host answers 404
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Flaky, directory=str(host)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def run(data, *args, sf=SF):
            env = {**os.environ, VAR: str(data)}
            return subprocess.run([sys.executable, "-I", str(ROOT / "python" / "kvevaluator" / "datasets" / "ldbc" / "fetch.py"), sf, "--host", url,
                                   "--manifest", str(manifest), "--retry-wait", "0", *args], capture_output=True, text=True, env=env)
        r = run(d / "pin", "--pin")
        check("a 404 is an answer, not retried", "attempt 2/" not in r.stdout, r.stdout + r.stderr)
        m = json.load(open(manifest)) if manifest.exists() else {"sfs": {}}
        e = m["sfs"].get(SF, {})
        check("pin: csv and params pinned with each file, the missing updates archive pinned as not hosted, each under LDBC's URL",
              r.returncode == 0 and e.get("csv", {}).get("hosted") is True and len(e["csv"]["files"]) == 2
              and e["csv"]["url"] == f"https://datasets.ldbcouncil.org/{csv_rel}"
              and len(e.get("params", {}).get("files", {})) == 1 and e.get("updates") == {"url": f"https://datasets.ldbcouncil.org/snb-interactive-v1-updates/social_network-sf{SF}-numpart-1.tar.zst", "hosted": False},
              r.stdout + r.stderr)
        snap = manifest.read_bytes()
        r = run(d / "pin", "--pin")
        check("a re-pin is refused, the manifest untouched", r.returncode != 0 and manifest.read_bytes() == snap, r.stdout + r.stderr)
        r = run(d / "a", sf="8")
        check("an SF not in the manifest is refused", r.returncode != 0 and "not in" in r.stdout + r.stderr, r.stdout + r.stderr)

        a = d / "a" / "datasets" / "ldbc"
        r = run(d / "a")
        check("fetch: the trees in place, the not-hosted archive skipped",
              r.returncode == 0 and (a / f"sf{SF}" / "CsvBasic-LongDateFormatter" / "static" / "place_0_0.csv").is_file()
              and (a / f"substitution_parameters-sf{SF}" / "interactive_1_param.txt").is_file()
              and "not hosted" in r.stdout and not (a / f"sf{SF}" / "update_streams").exists(), r.stdout + r.stderr)
        r = run(d / "a")
        check("a second fetch downloads nothing", r.returncode == 0 and "downloading" not in r.stdout
              and r.stdout.count("in place") == 2, r.stdout + r.stderr)
        r = run(d / "a", "--check")
        check("--check passes on the fetched trees", r.returncode == 0, r.stdout + r.stderr)
        r = run(d / "empty", "--check")
        check("--check fails on a data dir without them", r.returncode != 0 and "missing" in r.stdout, r.stdout + r.stderr)

        # the plants: each must fail, and nothing in place may change
        place = a / f"sf{SF}" / "CsvBasic-LongDateFormatter" / "static" / "place_0_0.csv"
        place.write_text("id|name\n1|y\n")
        r = run(d / "a")
        check("a changed file in place is refused and left as it is", r.returncode != 0 and "differs" in r.stdout
              and place.read_text() == "id|name\n1|y\n", r.stdout + r.stderr)
        place.write_text("id|name\n1|x\n")
        extra = a / f"substitution_parameters-sf{SF}" / "extra.txt"
        extra.write_text("x\n")
        r = run(d / "a", "--check")
        check("an extra file in place is refused", r.returncode != 0 and "not in the manifest" in r.stdout, r.stdout + r.stderr)
        extra.unlink()

        # a download that drops part way: retried, and named with the mirror advice when every attempt drops
        csv_tree = Path("datasets") / "ldbc" / f"sf{SF}" / "CsvBasic-LongDateFormatter"
        Flaky.drops = {f"/{csv_rel}": 1}
        r = run(d / "c")
        check("a download that drops once is retried and lands", r.returncode == 0 and "attempt 1/3 failed" in r.stdout
              and "ended after" in r.stdout and (d / "c" / csv_tree / "static" / "place_0_0.csv").is_file(), r.stdout + r.stderr)
        Flaky.drops = {f"/{csv_rel}": 3}
        r = run(d / "e")
        bad = [ln for ln in r.stdout.splitlines() if ln.startswith("  BAD")]
        check("three drops: refused, the line names the archive and the file:// mirror, nothing in place, no work dir",
              r.returncode != 0 and r.stdout.count("failed: the stream ended") == 2 and len(bad) == 1
              and f"sf{SF} csv: 3 attempts failed" in bad[0] and "--host file:///" in bad[0]
              and not (d / "e" / csv_tree).exists()
              and not [p for p in (d / "e" / "datasets" / "ldbc").iterdir() if p.name.startswith(".fetch-")], r.stdout + r.stderr)
        Flaky.drops = {}
        make_archive(host, csv_rel, f"social_network-sf{SF}-CsvBasic-LongDateFormatter",
                     {"static/place_0_0.csv": "id|name\n1|z\n", "dynamic/person_0_0.csv": "id\n7\n"})
        r = run(d / "b")
        b = d / "b" / "datasets" / "ldbc"
        check("a changed archive on the host is refused; nothing in place, no work dir left",
              r.returncode != 0 and "REFUSED" in r.stdout and not (b / f"sf{SF}" / "CsvBasic-LongDateFormatter").exists()
              and not [p for p in b.iterdir() if p.name.startswith(".fetch-")], r.stdout + r.stderr)
        r = run(d / "a")
        check("after the plants are undone, the trees in place check OK again", r.returncode == 0, r.stdout + r.stderr)
        srv.shutdown()
    ok = all(results)
    print(f"check_fetch_ldbc {'OK' if ok else 'FAIL'}: {sum(results)}/{len(results)} cases")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
