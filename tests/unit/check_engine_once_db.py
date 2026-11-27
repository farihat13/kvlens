"""engines/once.sh --db builds and loads only the named databases: in a temp copy with only Dgraph's binary (a stub),
stub fetch, build and data-link steps and a stub load, `--db dgraph` builds nothing and loads dgraph at env.sh's SF
(0.1); `--db all` builds NebulaGraph and ArangoDB (not Dgraph), then loads the three; `--db dgraph,arangodb` builds
ArangoDB only; an unknown db is refused before anything runs. The control: a build that leaves no binary stops before
any load.

    python tests/unit/check_engine_once_db.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator.paths import ROOT

E = ROOT / "engines"
STUB_LOAD = '#!/bin/bash\necho "STUB load-data.sh $*"\n'


BINARY = {"dgraph": "dgraph-kvstats", "nebulagraph": "nebula-graphd", "arangodb": "arangod",
          "tugraph": "libtugraph_kvstats.so"}     # TuGraph's release is fetched; what is built is our counter library


def engines_copy(d, builds=True):
    """a temp engines/ with once.sh, env.sh, check-tools.sh and needs.tsv, each DB's .env, Dgraph's binary (a stub), a
    stub load, and per DB a stub get.sh, build.sh (it makes the binary when `builds`) and justfile"""
    e = Path(d) / "engines"
    e.mkdir()
    for f in ("once.sh", "env.sh", "check-tools.sh", "needs.tsv"):
        shutil.copy(E / f, e / f)
    (e / "load-data.sh").write_text(STUB_LOAD)
    for db in BINARY:
        (e / db).mkdir()
        shutil.copy(E / db / ".env", e / db / ".env")
        (e / db / "get.sh").write_text(f'#!/bin/bash\necho "STUB get {db}"\n')
        make = f'mkdir -p "$(dirname "$0")/bin" && printf "#!/bin/sh\\n" > "$(dirname "$0")/bin/{BINARY[db]}" && chmod +x "$(dirname "$0")/bin/{BINARY[db]}"'
        (e / db / "build.sh").write_text(f'#!/bin/bash\necho "STUB build {db} $*"\n' + (make + "\n" if builds else ""))
        (e / db / "justfile").write_text(f'ldbc-download sf:\n    @echo "STUB ldbc-download {db} {{{{sf}}}}"\n'
                                         f'setup-console:\n    @echo "STUB console"\nsetup-nebula-importer:\n    @echo "STUB importer"\n')
    b = e / "dgraph" / "bin" / BINARY["dgraph"]
    b.parent.mkdir()
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    return e


def run(e, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("SF", "COSTEVAL_SF")}
    env["LDBC_SNB_DATA_DIR"] = "/nonexistent/ldbc"      # env.sh then needs no Python
    p = subprocess.run(["bash", str(e / "once.sh"), *args], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout + p.stderr


def main():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail.strip()[-400:]}"))

    with tempfile.TemporaryDirectory() as d:
        e = engines_copy(d)
        rc, out = run(e, "--db", "bogus")
        check("--db bogus: refused, nothing run", rc == 1 and "unknown db: bogus" in out and "STUB" not in out, out)
        rc, out = run(e, "--db", "dgraph,bogus")
        check("--db dgraph,bogus: refused before any step", rc == 1 and "unknown db: bogus" in out and "STUB" not in out, out)
        rc, out = run(e, "--db", "dgraph")
        check("--db dgraph with Dgraph's binary: nothing built, its data linked, dgraph loaded at SF 0.1",
              rc == 0 and "ok    [dgraph]" in out and "MISS" not in out and "STUB build" not in out
              and "STUB ldbc-download dgraph 0.1" in out and "STUB load-data.sh --db dgraph --sf 0.1" in out, out)
        rc, out = run(e, "--db", "dgraph,arangodb")
        check("--db dgraph,arangodb: ArangoDB fetched and built, NebulaGraph untouched, then the load",
              rc == 0 and "STUB get arangodb" in out and "STUB build arangodb kvstats" in out and "nebula" not in out
              and "STUB build dgraph" not in out and "STUB load-data.sh --db dgraph,arangodb" in out, out)
        rc, out = run(e, "--db", "all")
        check("--db all: NebulaGraph built with its console and importer (ArangoDB's binary is there now), then the load",
              rc == 0 and "STUB build nebulagraph kvstats" in out and "STUB console" in out and "STUB importer" in out
              and "STUB build arangodb" not in out and out.index("STUB build nebulagraph") < out.index("STUB load-data.sh --db all"), out)
        check("--db all: TuGraph is not among them (it has no evaluator format; it is set up by name)", "tugraph" not in out, out)
        rc, out = run(e, "--db", "tugraph")
        check("--db tugraph: TuGraph fetched, its counter library built, then its load",
              rc == 0 and "STUB get tugraph" in out and "STUB build tugraph kvstats" in out
              and "STUB ldbc-download tugraph 0.1" in out and "STUB load-data.sh --db tugraph --sf 0.1" in out, out)
        rc, out = run(e, "--db", "tugraph-detached")
        check("--db tugraph-detached: TuGraph's second configuration, in TuGraph's dir (nothing built again), its own load",
              rc == 0 and "ok    [tugraph-detached]" in out and "STUB build" not in out
              and "STUB ldbc-download tugraph 0.1" in out and "STUB load-data.sh --db tugraph-detached --sf 0.1" in out, out)
    with tempfile.TemporaryDirectory() as d:
        rc, out = run(engines_copy(d, builds=False), "--db", "all")
        check("control: a build that leaves no binary stops before any load",
              rc == 1 and "FAIL  [nebula] no kvstats binary" in out and "STUB load-data.sh" not in out, out)
    bad = results.count(False)
    print(f"check_engine_once_db {'OK' if not bad else 'FAIL'}: {len(results) - bad}/{len(results)} cases")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
